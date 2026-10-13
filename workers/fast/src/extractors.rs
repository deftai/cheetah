//! Platform-specific extractors and the fallback chain that runs them.
//!
//! A job may name a `strategy`. The chain is: the strategy's extractor (if this
//! worker has one), then a generic page fetch. An extractor failure never fails
//! the job on its own; it is reported as `fallback_from` on the generic result.
//! The chain is straight-line, with at most one extractor attempt and one
//! generic fetch, so it cannot loop.

pub mod github;

use crate::{fetch_page, FetchConfig, PageResult, Result};
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

/// Extractor registry. Slices that add a platform register it here; an
/// unregistered strategy is ignored and the page is fetched generically.
pub fn lookup(strategy: &str) -> Option<ExtractorFn> {
    match strategy {
        "github_repo" => Some(github::extract),
        _ => None,
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
    fn registry_knows_only_implemented_strategies() {
        assert!(lookup("github_repo").is_some());
        assert!(lookup("hn_thread").is_none());
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
