//! Platform-specific extractors and the fallback chain that runs them.
//!
//! A job may name a `strategy`. The chain is: the strategy's extractor (if this
//! worker has one), then a generic page fetch. An extractor failure never fails
//! the job on its own; it is reported as `fallback_from` on the generic result.
//! The chain is straight-line, with at most one extractor attempt and one
//! generic fetch, so it cannot loop.

pub mod github;
pub mod hn;

use crate::{fetch_page, Error, FetchConfig, PageResult, Result};
use futures_util::future::BoxFuture;
use reqwest::Client;
use std::future::Future;
use std::net::IpAddr;
use tracing::warn;

/// Strategy name reported for a plain page fetch.
pub const STRATEGY_GENERIC: &str = "generic";

pub type ExtractorFn = for<'a> fn(
    &'a Client,
    &'a str,
    &'a FetchConfig,
    Option<IpAddr>,
) -> BoxFuture<'a, Result<PageResult>>;

/// Maps any error to `Error::Extractor` with a fixed message.
fn ex<E>(msg: &'static str) -> impl FnOnce(E) -> Error {
    move |_| Error::Extractor(msg.to_string())
}

fn fail<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error::Extractor(msg.into()))
}

/// Collapses control characters (newlines included) to spaces: titles come from
/// third parties and must stay on one line.
fn one_line(s: &str) -> String {
    // U+2028/U+2029 are not control characters but still end a line for Python's
    // `splitlines()` and some Markdown renderers.
    s.chars()
        .map(|c| if c.is_control() || matches!(c, '\u{2028}' | '\u{2029}') { ' ' } else { c })
        .collect()
}

/// Extractor registry. Slices that add a platform register it here; an
/// unregistered strategy is ignored and the page is fetched generically.
pub fn lookup(strategy: &str) -> Option<ExtractorFn> {
    match strategy {
        "github_repo" => Some(github::extract),
        "hn_thread" => Some(hn::extract),
        _ => None,
    }
}

/// Extractors produce Markdown or plain text, not HTML, so a caller that asked
/// for `format: "html"` is served by the generic fetch instead.
pub fn strategy_for_format<'a>(strategy: Option<&'a str>, format: Option<&str>) -> Option<&'a str> {
    match format {
        Some(f) if f.eq_ignore_ascii_case("html") => None,
        _ => strategy,
    }
}

#[derive(Debug)]
pub struct ChainOutcome {
    pub result: Result<PageResult>,
    /// What produced `result`: the extractor's strategy name, or "generic".
    pub strategy: String,
    /// Set when an extractor was tried, failed, and the generic fetch ran instead.
    pub fallback_from: Option<String>,
}

/// Runs the extractor for `requested` (when one is registered), falling back to
/// `generic` if it fails.
pub async fn run_chain<G, Fut>(
    client: &Client,
    url: &str,
    config: &FetchConfig,
    pinned_ip: Option<IpAddr>,
    requested: Option<&str>,
    lookup: impl Fn(&str) -> Option<ExtractorFn>,
    generic: G,
) -> ChainOutcome
where
    G: FnOnce() -> Fut,
    Fut: Future<Output = Result<PageResult>>,
{
    let mut fallback_from = None;
    if let Some(name) = requested {
        if let Some(extract) = lookup(name) {
            match extract(client, url, config, pinned_ip).await {
                Ok(page) => {
                    return ChainOutcome {
                        result: Ok(page),
                        strategy: name.to_string(),
                        fallback_from: None,
                    }
                }
                Err(e) => {
                    warn!(strategy = name, error = %e, "extractor failed, falling back to generic fetch");
                    fallback_from = Some(name.to_string());
                }
            }
        }
    }
    ChainOutcome {
        result: generic().await,
        strategy: STRATEGY_GENERIC.to_string(),
        fallback_from,
    }
}

/// The production generic step: a plain page fetch.
pub async fn generic_fetch(
    client: &Client,
    url: &str,
    config: &FetchConfig,
    pinned_ip: Option<IpAddr>,
) -> Result<PageResult> {
    fetch_page(client, url, config, pinned_ip).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Error;
    use chrono::Utc;

    fn page(tag: &str) -> PageResult {
        PageResult {
            url: "https://example.com".into(),
            title: Some(tag.into()),
            description: None,
            language: None,
            status_code: 200,
            markdown: tag.into(),
            raw_html: tag.into(),
            fetched_at: Utc::now(),
        }
    }

    fn ok_extractor<'a>(
        _: &'a Client,
        _: &'a str,
        _: &'a FetchConfig,
        _: Option<IpAddr>,
    ) -> BoxFuture<'a, Result<PageResult>> {
        Box::pin(async { Ok(page("extractor")) })
    }

    fn failing_extractor<'a>(
        _: &'a Client,
        _: &'a str,
        _: &'a FetchConfig,
        _: Option<IpAddr>,
    ) -> BoxFuture<'a, Result<PageResult>> {
        Box::pin(async { Err(Error::SsrfBlocked) })
    }

    async fn run(
        requested: Option<&str>,
        lookup: impl Fn(&str) -> Option<ExtractorFn>,
    ) -> (ChainOutcome, std::sync::atomic::AtomicUsize) {
        let calls = std::sync::atomic::AtomicUsize::new(0);
        let out = run_chain(
            &Client::new(),
            "https://example.com",
            &FetchConfig::default(),
            None,
            requested,
            lookup,
            || async {
                calls.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok(page("generic"))
            },
        )
        .await;
        (out, calls)
    }

    #[test]
    fn html_requests_skip_platform_strategies() {
        assert_eq!(strategy_for_format(Some("github_repo"), Some("html")), None);
        assert_eq!(strategy_for_format(Some("github_repo"), Some("HTML")), None);
        assert_eq!(strategy_for_format(Some("github_repo"), Some("markdown")), Some("github_repo"));
        assert_eq!(strategy_for_format(Some("github_repo"), Some("text")), Some("github_repo"));
        assert_eq!(strategy_for_format(Some("github_repo"), None), Some("github_repo"));
        assert_eq!(strategy_for_format(None, Some("html")), None);
    }

    #[test]
    fn registry_resolves_known_strategies_only() {
        assert!(lookup("github_repo").is_some());
        assert!(lookup("hn_thread").is_some());
        assert!(lookup("no_such_strategy").is_none());
        assert!(lookup("").is_none());
    }

    #[tokio::test]
    async fn no_strategy_runs_generic_only() {
        let (out, calls) = run(None, |_| Some(ok_extractor as ExtractorFn)).await;
        assert_eq!(out.strategy, STRATEGY_GENERIC);
        assert_eq!(out.fallback_from, None);
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn unregistered_strategy_is_ignored_not_a_fallback() {
        let (out, calls) = run(Some("github_repo"), |_| None).await;
        assert_eq!(out.strategy, STRATEGY_GENERIC);
        assert_eq!(out.fallback_from, None);
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn extractor_success_skips_generic() {
        let (out, calls) = run(Some("github_repo"), |_| Some(ok_extractor as ExtractorFn)).await;
        assert_eq!(out.strategy, "github_repo");
        assert_eq!(out.fallback_from, None);
        assert_eq!(out.result.unwrap().title.as_deref(), Some("extractor"));
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn extractor_failure_falls_back_to_generic_once() {
        let (out, calls) = run(Some("github_repo"), |_| {
            Some(failing_extractor as ExtractorFn)
        })
        .await;
        assert_eq!(out.strategy, STRATEGY_GENERIC);
        assert_eq!(out.fallback_from.as_deref(), Some("github_repo"));
        assert_eq!(out.result.unwrap().title.as_deref(), Some("generic"));
        assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
    }

    #[tokio::test]
    async fn extractor_and_generic_failure_still_reports_fallback() {
        let out = run_chain(
            &Client::new(),
            "https://example.com",
            &FetchConfig::default(),
            None,
            Some("hn_thread"),
            |_| Some(failing_extractor as ExtractorFn),
            || async { Err(Error::SsrfBlocked) },
        )
        .await;
        assert!(out.result.is_err());
        assert_eq!(out.fallback_from.as_deref(), Some("hn_thread"));
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    pub(crate) type Seen = Arc<Mutex<Vec<String>>>;

    /// Serves canned `(status, body)` replies keyed by request path prefix and
    /// records each raw request so tests can inspect headers.
    pub(crate) async fn fake_api(routes: Vec<(&'static str, u16, &'static [u8])>) -> (String, Seen) {
        fake_api_with_json(routes, &[]).await
    }

    /// Like `fake_api`, but replies under `json_prefixes` always use the JSON
    /// content type, as the real contents API does for directories.
    pub(crate) async fn fake_api_with_json(
        routes: Vec<(&'static str, u16, &'static [u8])>,
        json_prefixes: &'static [&'static str],
    ) -> (String, Seen) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let seen: Seen = Arc::new(Mutex::new(vec![]));
        let seen2 = seen.clone();
        tokio::spawn(async move {
            loop {
                let (mut sock, _) = match listener.accept().await {
                    Ok(x) => x,
                    Err(_) => return,
                };
                let routes = routes.clone();
                let seen = seen2.clone();
                tokio::spawn(async move {
                    let mut buf = vec![0u8; 8192];
                    let n = sock.read(&mut buf).await.unwrap_or(0);
                    let req = String::from_utf8_lossy(&buf[..n]).to_string();
                    let path = req.split_whitespace().nth(1).unwrap_or("").to_string();
                    let wants_raw = req.to_ascii_lowercase().contains("accept: application/vnd.github.raw");
                    let raw = wants_raw && !json_prefixes.iter().any(|p| path.starts_with(p));
                    let content_type = if raw {
                        "application/vnd.github.raw+json; charset=utf-8"
                    } else {
                        "application/json; charset=utf-8"
                    };
                    seen.lock().unwrap().push(req);
                    let (status, body) = routes
                        .iter()
                        .find(|(p, _, _)| path.starts_with(p))
                        .map(|(_, s, b)| (*s, *b))
                        .unwrap_or((404, b"not found"));
                    let head = format!(
                        "HTTP/1.1 {status} X\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    let _ = sock.write_all(head.as_bytes()).await;
                    let _ = sock.write_all(body).await;
                });
            }
        });
        (base, seen)
    }


}
