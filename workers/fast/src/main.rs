use cheetah_fast::extractors::{self, generic_fetch};
use cheetah_fast::{FetchConfig, Secret};
use clap::Parser;
use reqwest::Client;
use serde::Serialize;
use std::net::IpAddr;
use std::sync::Arc;
use tokio::signal;
use tokio::sync::Semaphore;
use tracing::{error, info, warn};

#[derive(Serialize)]
struct StatusPayload<'a> {
    job_id: &'a str,
    status: &'static str,
    pages_total: u32,
    pages_completed: u32,
    results: Vec<ResultEntry<'a>>,
}

#[derive(Serialize)]
struct ResultEntry<'a> {
    success: bool,
    data: ResultData<'a>,
}

#[derive(Serialize)]
struct ResultData<'a> {
    content: &'a str,
    markdown: &'a str,
    metadata: ResultMetadata<'a>,
}

#[derive(Serialize)]
struct ResultMetadata<'a> {
    title: &'a str,
    description: &'a str,
    language: &'a str,
    status_code: u16,
    strategy: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    fallback_from: Option<&'a str>,
}

#[derive(Parser, Debug)]
#[command(name = "cheetah-fast", about = "Fast static page scraper worker")]
struct Args {
    #[arg(long, env = "REDIS_URL", default_value = "redis://127.0.0.1:6379")]
    redis_url: String,

    #[arg(long, default_value_t = 50)]
    concurrency: usize,

    #[arg(long, default_value = "fast-workers")]
    group_name: String,

    #[arg(long, default_value = "scrape_jobs")]
    stream: String,

    #[arg(long, default_value = "job_results")]
    results_prefix: String,

    /// Optional GitHub API token that raises the GitHub extractor's rate limit.
    /// Prefer the GITHUB_TOKEN environment variable: a flag value is visible in
    /// `ps`. Use a fine-grained read-only token; the extractor refuses
    /// non-public repositories either way.
    #[arg(long, env = "GITHUB_TOKEN", hide_env_values = true)]
    github_token: Option<Secret>,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "cheetah_fast=info".into()),
        )
        .init();

    let args = Args::parse();
    let consumer_name = format!("worker-{}", uuid::Uuid::new_v4());

    info!(
        redis = %args.redis_url,
        concurrency = args.concurrency,
        group = %args.group_name,
        consumer = %consumer_name,
        "starting cheetah-fast worker"
    );

    let redis_client = redis::Client::open(args.redis_url.as_str())?;
    let mut conn = redis_client.get_multiplexed_async_connection().await?;

    let _: Result<(), _> = redis::cmd("XGROUP")
        .arg("CREATE")
        .arg(&args.stream)
        .arg(&args.group_name)
        .arg("0")
        .arg("MKSTREAM")
        .query_async(&mut conn)
        .await;

    let http_client = Client::builder()
        .pool_max_idle_per_host(20)
        .redirect(reqwest::redirect::Policy::none())
        .build()?;
    let config = Arc::new(FetchConfig {
        github_token: args.github_token.filter(|t| !t.expose().is_empty()),
        ..FetchConfig::default()
    });
    let semaphore = Arc::new(Semaphore::new(args.concurrency));

    let (shutdown_tx, shutdown_rx) = tokio::sync::watch::channel(false);

    tokio::spawn(async move {
        let ctrl_c = signal::ctrl_c();
        let mut sigterm = signal::unix::signal(signal::unix::SignalKind::terminate())
            .expect("failed to register SIGTERM handler");

        tokio::select! {
            _ = ctrl_c => info!("received SIGINT, shutting down"),
            _ = sigterm.recv() => info!("received SIGTERM, shutting down"),
        }
        let _ = shutdown_tx.send(true);
    });

    info!("worker ready, consuming from stream '{}'", args.stream);

    loop {
        if *shutdown_rx.borrow() {
            break;
        }

        let result: redis::RedisResult<redis::streams::StreamReadReply> = redis::cmd("XREADGROUP")
            .arg("GROUP")
            .arg(&args.group_name)
            .arg(&consumer_name)
            .arg("COUNT")
            .arg(10)
            .arg("BLOCK")
            .arg(2000_u64)
            .arg("STREAMS")
            .arg(&args.stream)
            .arg(">")
            .query_async(&mut conn)
            .await;

        let reply = match result {
            Ok(r) => r,
            Err(e) => {
                warn!(error = %e, "XREADGROUP failed, retrying");
                tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                continue;
            }
        };

        for stream_key in &reply.keys {
            for entry in &stream_key.ids {
                let entry_id = entry.id.clone();
                let url = match entry.map.get("url") {
                    Some(redis::Value::BulkString(bytes)) => {
                        String::from_utf8_lossy(bytes).to_string()
                    }
                    _ => {
                        warn!(id = %entry_id, "job missing 'url' field, skipping");
                        let _: Result<(), _> = redis::cmd("XACK")
                            .arg(&args.stream)
                            .arg(&args.group_name)
                            .arg(&entry_id)
                            .query_async(&mut conn)
                            .await;
                        continue;
                    }
                };

                let job_id = entry
                    .map
                    .get("job_id")
                    .and_then(|v| match v {
                        redis::Value::BulkString(bytes) => {
                            Some(String::from_utf8_lossy(bytes).to_string())
                        }
                        _ => None,
                    })
                    .unwrap_or_else(|| entry_id.clone());

                let pinned_ip: Option<IpAddr> = entry
                    .map
                    .get("resolved_ip")
                    .and_then(|v| match v {
                        redis::Value::BulkString(bytes) => {
                            let s = String::from_utf8_lossy(bytes);
                            s.parse().ok()
                        }
                        _ => None,
                    });

                // Strategies this worker has no extractor for are ignored: the
                // page is fetched generically and reported as such.
                let requested_strategy = entry.map.get("strategy").and_then(|v| match v {
                    redis::Value::BulkString(bytes) if !bytes.is_empty() => {
                        Some(String::from_utf8_lossy(bytes).to_string())
                    }
                    _ => None,
                });

                let format = entry.map.get("format").and_then(|v| match v {
                    redis::Value::BulkString(bytes) => Some(String::from_utf8_lossy(bytes).to_string()),
                    _ => None,
                });
                let requested_strategy = requested_strategy
                    .as_deref()
                    .and_then(|s| extractors::strategy_for_format(Some(s), format.as_deref()))
                    .map(str::to_string);

                let permit = semaphore.clone().acquire_owned().await.unwrap();
                let client = http_client.clone();
                let cfg = config.clone();
                let mut task_conn = conn.clone();
                let stream_name = args.stream.clone();
                let group = args.group_name.clone();
                let results_prefix = args.results_prefix.clone();

                tokio::spawn(async move {
                    let _permit = permit;
                    info!(url = %url, job_id = %job_id, pinned_ip = ?pinned_ip, requested_strategy = ?requested_strategy, "processing job");

                    let outcome = extractors::run_chain(
                        &client,
                        &url,
                        &cfg,
                        pinned_ip,
                        requested_strategy.as_deref(),
                        extractors::lookup,
                        || generic_fetch(&client, &url, &cfg, pinned_ip),
                    )
                    .await;
                    let result = outcome.result;
                    let result_key = format!("{}:{}", results_prefix, job_id);

                    let (payload, title_buf, desc_buf, lang_buf);
                    let serialized = match result {
                        Ok(page) => {
                            info!(url = %url, job_id = %job_id, status = page.status_code, "job complete");
                            title_buf = page.title.unwrap_or_default();
                            desc_buf = page.description.unwrap_or_default();
                            lang_buf = page.language.unwrap_or_default();
                            payload = StatusPayload {
                                job_id: &job_id,
                                status: "completed",
                                pages_total: 1,
                                pages_completed: 1,
                                results: vec![ResultEntry {
                                    success: true,
                                    data: ResultData {
                                        content: &page.raw_html,
                                        markdown: &page.markdown,
                                        metadata: ResultMetadata {
                                            title: &title_buf,
                                            description: &desc_buf,
                                            language: &lang_buf,
                                            status_code: page.status_code,
                                            strategy: &outcome.strategy,
                                            fallback_from: outcome.fallback_from.as_deref(),
                                        },
                                    },
                                }],
                            };
                            serde_json::to_string(&payload).unwrap_or_default()
                        }
                        Err(e) => {
                            error!(url = %url, job_id = %job_id, error = %e, "job failed");
                            payload = StatusPayload {
                                job_id: &job_id,
                                status: "failed",
                                pages_total: 1,
                                pages_completed: 0,
                                results: vec![],
                            };
                            serde_json::to_string(&payload).unwrap_or_default()
                        }
                    };

                    let _: Result<(), _> = redis::cmd("SET")
                        .arg(&result_key)
                        .arg(&serialized)
                        .arg("EX")
                        .arg(3600_u64)
                        .query_async(&mut task_conn)
                        .await;

                    let _: Result<(), _> = redis::cmd("XACK")
                        .arg(&stream_name)
                        .arg(&group)
                        .arg(&entry_id)
                        .query_async(&mut task_conn)
                        .await;
                });
            }
        }
    }

    info!("shutdown complete");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn result_metadata_reports_strategy() {
        let m = ResultMetadata {
            title: "t",
            description: "",
            language: "",
            status_code: 200,
            strategy: "generic",
            fallback_from: None,
        };
        let v: serde_json::Value = serde_json::to_value(&m).unwrap();
        assert_eq!(v["strategy"], "generic");
        assert!(v.get("fallback_from").is_none());

        let m = ResultMetadata {
            fallback_from: Some("github_repo"),
            ..m
        };
        let v: serde_json::Value = serde_json::to_value(&m).unwrap();
        assert_eq!(v["fallback_from"], "github_repo");
    }
}
