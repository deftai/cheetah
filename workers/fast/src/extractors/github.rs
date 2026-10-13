//! GitHub extractor: repo README, file contents and issue/PR threads through
//! the REST API instead of scraping github.com HTML.
//!
//! Only `https://api.github.com` is ever contacted: the target URL is parsed
//! into an allow-listed request shape and its own host is never fetched.
//! (There is no DNS/private-IP pre-check: the host is a constant and TLS
//! certificate verification pins its identity.) The optional token is sent to
//! that host only, and only for repositories that are public, so a token with
//! private access cannot be used to read private content through this worker.
//! Any failure (unsupported URL, rate limit, 404, oversize or binary content)
//! surfaces as `Error::Extractor`, so the chain falls back to the generic fetch.
//!
//! Known limitations: a `blob` URL whose branch name contains `/` is split at
//! the first segment, so it 404s (and falls back) or, rarely, reads another
//! branch's file; issue threads show at most the first 100 comments (noted in
//! the output); PRs show the conversation only, not reviews or the diff.

use crate::{read_body_capped, Error, FetchConfig, PageResult, Result};
use chrono::Utc;
use futures_util::future::BoxFuture;
use percent_encoding::percent_decode_str;
use reqwest::{Client, Url};
use serde_json::Value;
use std::fmt::Write as _;
use std::net::IpAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

pub const API_BASE: &str = "https://api.github.com";
const API_HOST: &str = "api.github.com";
const ACCEPT_JSON: &str = "application/vnd.github+json";
const ACCEPT_RAW: &str = "application/vnd.github.raw+json";

/// GitHub responses are capped well below the generic page limit: comment
/// threads become a JSON tree plus rendered text, several times their size.
const MAX_API_BODY: usize = 2 * 1024 * 1024;
const COMMENTS_PER_PAGE: usize = 100;
/// Longest pause taken after a rate-limit response, whatever GitHub asks for.
const MAX_COOLDOWN_SECS: u64 = 3600;

/// Unix time before which API calls are skipped because GitHub rate limited us.
static RATE_LIMITED_UNTIL: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, PartialEq)]
pub(super) enum Target {
    Readme {
        owner: String,
        repo: String,
    },
    File {
        owner: String,
        repo: String,
        git_ref: String,
        path: Vec<String>,
    },
    Issue {
        owner: String,
        repo: String,
        number: u64,
    },
}

fn ex<E>(msg: &'static str) -> impl FnOnce(E) -> Error {
    move |_| Error::Extractor(msg.to_string())
}

fn fail<T>(msg: impl Into<String>) -> Result<T> {
    Err(Error::Extractor(msg.into()))
}

fn valid_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 100
        && s != "."
        && s != ".."
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'_' || b == b'-')
}

/// Decodes one path or ref segment. Anything that could escape its position
/// once re-encoded (separators, dot segments, control characters) is refused.
fn decode_segment(s: &str) -> Option<String> {
    let decoded = percent_decode_str(s).decode_utf8().ok()?;
    let bad = decoded.is_empty()
        || decoded == "."
        || decoded == ".."
        || decoded.chars().any(|c| c == '/' || c == '\\' || c.is_control());
    (!bad).then(|| decoded.into_owned())
}

pub(super) fn parse_target(raw: &str) -> Result<Target> {
    let url = Url::parse(raw).map_err(ex("unparseable URL"))?;
    if url.scheme() != "https" || !matches!(url.host_str(), Some("github.com" | "www.github.com")) {
        return fail("not a github.com URL");
    }
    let segs: Vec<&str> = url
        .path_segments()
        .map(|s| s.filter(|p| !p.is_empty()).collect())
        .unwrap_or_default();
    if segs.len() < 2 || !valid_name(segs[0]) {
        return fail("not a repository URL");
    }
    let repo_seg = segs[1].strip_suffix(".git").unwrap_or(segs[1]);
    if !valid_name(repo_seg) {
        return fail("not a repository URL");
    }
    let (owner, repo) = (segs[0].to_string(), repo_seg.to_string());
    match &segs[2..] {
        [] => Ok(Target::Readme { owner, repo }),
        ["blob", git_ref, path @ ..] if !path.is_empty() => {
            let git_ref = decode_segment(git_ref).ok_or(()).map_err(ex("unsupported file ref"))?;
            let path = path
                .iter()
                .map(|p| decode_segment(p))
                .collect::<Option<Vec<_>>>()
                .ok_or(())
                .map_err(ex("unsupported file path"))?;
            Ok(Target::File { owner, repo, git_ref, path })
        }
        ["issues" | "pull", n] if n.bytes().all(|b| b.is_ascii_digit()) => {
            let number = n.parse().map_err(ex("bad issue number"))?;
            Ok(Target::Issue { owner, repo, number })
        }
        _ => fail("unsupported GitHub URL"),
    }
}

fn build_url(base: &str, segments: &[&str], query: &[(&str, &str)]) -> Result<Url> {
    let mut url = Url::parse(base).map_err(ex("bad API base"))?;
    url.path_segments_mut()
        .map_err(ex("bad API base"))?
        .extend(segments);
    if !query.is_empty() {
        url.query_pairs_mut().extend_pairs(query);
    }
    Ok(url)
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// When a response says we are rate limited, returns the unix time to pause
/// until. A bare 403 (blocked or private repo) is not a rate limit.
fn cooldown_until(status: u16, headers: &reqwest::header::HeaderMap, now: u64) -> Option<u64> {
    if status != 403 && status != 429 {
        return None;
    }
    let num = |name: &str| {
        headers
            .get(name)
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.trim().parse::<u64>().ok())
    };
    let until = if let Some(secs) = num("retry-after") {
        now.saturating_add(secs)
    } else if num("x-ratelimit-remaining") == Some(0) {
        num("x-ratelimit-reset").unwrap_or(now + 60)
    } else if status == 429 {
        now + 60
    } else {
        return None;
    };
    Some(until.clamp(now + 1, now + MAX_COOLDOWN_SECS))
}

/// Returns the body of a successful response, or an extractor error.
async fn api_get(client: &Client, url: Url, accept: &str, config: &FetchConfig) -> Result<Vec<u8>> {
    if now_secs() < RATE_LIMITED_UNTIL.load(Ordering::Relaxed) {
        return fail("GitHub API rate limit cooldown in effect");
    }
    // Defence in depth: every URL here is built from API_BASE, so this only
    // ever withholds the token from a non-GitHub host (as the tests do).
    let send_token = url.host_str() == Some(API_HOST);
    let mut req = client
        .get(url)
        .header("User-Agent", &config.user_agent)
        .header("Accept", accept)
        .header("X-GitHub-Api-Version", "2022-11-28")
        .timeout(config.timeout);
    if let (true, Some(token)) = (send_token, &config.github_token) {
        req = req.bearer_auth(token.expose());
    }
    let resp = req.send().await?;
    let status = resp.status();
    if let Some(until) = cooldown_until(status.as_u16(), resp.headers(), now_secs()) {
        RATE_LIMITED_UNTIL.fetch_max(until, Ordering::Relaxed);
        return fail(format!("GitHub API rate limited ({status})"));
    }
    if !status.is_success() {
        return fail(format!("GitHub API returned {status}"));
    }
    read_body_capped(resp, config.max_body_size.min(MAX_API_BODY)).await
}

/// With a token configured, only public repositories are read: the token may
/// have private access that job submitters must not be able to reach.
async fn ensure_public(
    base: &str,
    client: &Client,
    owner: &str,
    repo: &str,
    config: &FetchConfig,
) -> Result<()> {
    if config.github_token.is_none() {
        return Ok(());
    }
    let url = build_url(base, &["repos", owner, repo], &[])?;
    let body = api_get(client, url, ACCEPT_JSON, config).await?;
    let meta: Value = serde_json::from_slice(&body).map_err(ex("bad repository JSON"))?;
    if meta.get("private").and_then(Value::as_bool) == Some(false) {
        Ok(())
    } else {
        fail("repository is not public")
    }
}

fn utf8_text(body: Vec<u8>) -> Result<String> {
    let text = String::from_utf8(body).map_err(ex("content is not UTF-8 text"))?;
    if text.trim().is_empty() {
        return fail("content is empty");
    }
    Ok(text)
}

/// Wraps text in a code fence long enough that the text cannot close it.
fn fenced(text: &str) -> String {
    let (mut longest, mut run) = (0, 0);
    for b in text.bytes() {
        if b == b'`' {
            run += 1;
            longest = longest.max(run);
        } else {
            run = 0;
        }
    }
    let fence = "`".repeat(longest.max(2) + 1);
    let mut out = String::with_capacity(text.len() + 2 * fence.len() + 2);
    out.push_str(&fence);
    out.push('\n');
    out.push_str(text);
    out.push('\n');
    out.push_str(&fence);
    out
}

fn str_field<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn login(v: &Value) -> &str {
    v.pointer("/user/login").and_then(Value::as_str).unwrap_or("ghost")
}

/// A one-line title: GitHub titles are user-controlled and may hold newlines.
fn one_line(s: &str) -> String {
    s.chars().map(|c| if c.is_control() { ' ' } else { c }).collect()
}

pub(super) fn render_issue(issue: &Value, comments: &Value) -> String {
    let number = issue.get("number").and_then(Value::as_u64).unwrap_or(0);
    let mut out = String::new();
    let _ = write!(out, "# {} (#{number})\n\n", one_line(str_field(issue, "title")));
    let kind = if issue.get("pull_request").is_some() { "pull request" } else { "issue" };
    let _ = write!(out, "{kind} · {} · opened by @{}", str_field(issue, "state"), login(issue));
    if let Some(labels) = issue.get("labels").and_then(Value::as_array) {
        let names: Vec<&str> = labels
            .iter()
            .filter_map(|l| l.get("name").and_then(Value::as_str))
            .collect();
        if !names.is_empty() {
            let _ = write!(out, " · labels: {}", names.join(", "));
        }
    }
    out.push_str("\n\n");
    out.push_str(str_field(issue, "body"));
    if let Some(list) = comments.as_array().filter(|l| !l.is_empty()) {
        out.push_str("\n\n## Comments\n");
        for c in list {
            let _ = write!(
                out,
                "\n### @{} · {}\n\n{}\n",
                login(c),
                str_field(c, "created_at"),
                str_field(c, "body")
            );
        }
        if list.len() >= COMMENTS_PER_PAGE {
            let _ = write!(
                out,
                "\n_Showing the first {COMMENTS_PER_PAGE} comments; later ones are omitted._\n"
            );
        }
    }
    out
}

pub(super) async fn extract_with_base(
    base: &str,
    client: &Client,
    url: &str,
    config: &FetchConfig,
) -> Result<PageResult> {
    let (title, markdown, raw) = match parse_target(url)? {
        Target::Readme { owner, repo } => {
            ensure_public(base, client, &owner, &repo, config).await?;
            let api = build_url(base, &["repos", &owner, &repo, "readme"], &[])?;
            let text = utf8_text(api_get(client, api, ACCEPT_RAW, config).await?)?;
            (format!("{owner}/{repo}"), text.clone(), text)
        }
        Target::File { owner, repo, git_ref, path } => {
            ensure_public(base, client, &owner, &repo, config).await?;
            let mut segs = vec!["repos", owner.as_str(), repo.as_str(), "contents"];
            segs.extend(path.iter().map(String::as_str));
            let api = build_url(base, &segs, &[("ref", &git_ref)])?;
            let text = utf8_text(api_get(client, api, ACCEPT_RAW, config).await?)?;
            let name = path.join("/");
            let md = if name.to_ascii_lowercase().ends_with(".md") {
                text.clone()
            } else {
                fenced(&text)
            };
            (one_line(&format!("{owner}/{repo}: {name}")), md, text)
        }
        Target::Issue { owner, repo, number } => {
            ensure_public(base, client, &owner, &repo, config).await?;
            let n = number.to_string();
            let per_page = COMMENTS_PER_PAGE.to_string();
            let issue_url = build_url(base, &["repos", &owner, &repo, "issues", &n], &[])?;
            let comments_url = build_url(
                base,
                &["repos", &owner, &repo, "issues", &n, "comments"],
                &[("per_page", &per_page)],
            )?;
            let (issue_raw, comments_raw) = tokio::try_join!(
                api_get(client, issue_url, ACCEPT_JSON, config),
                api_get(client, comments_url, ACCEPT_JSON, config),
            )?;
            let issue: Value = serde_json::from_slice(&issue_raw).map_err(ex("bad issue JSON"))?;
            let comments: Value =
                serde_json::from_slice(&comments_raw).map_err(ex("bad comments JSON"))?;
            let md = render_issue(&issue, &comments);
            let title = format!("{} (#{number})", one_line(str_field(&issue, "title")));
            // The raw JSON also carries emails, avatar URLs and node ids for
            // every participant; keep only the rendered text.
            let raw = md.clone();
            (title, md, raw)
        }
    };
    Ok(PageResult {
        url: url.to_string(),
        title: Some(title),
        description: None,
        language: None,
        status_code: 200,
        markdown,
        raw_html: raw,
        fetched_at: Utc::now(),
    })
}

/// Registered extractor entry point. `_pinned_ip` belongs to the submitted
/// URL's host, which this extractor never contacts.
pub fn extract<'a>(
    client: &'a Client,
    url: &'a str,
    config: &'a FetchConfig,
    _pinned_ip: Option<IpAddr>,
) -> BoxFuture<'a, Result<PageResult>> {
    Box::pin(extract_with_base(API_BASE, client, url, config))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Secret;
    use reqwest::header::{HeaderMap, HeaderValue};
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn readme(owner: &str, repo: &str) -> Target {
        Target::Readme { owner: owner.into(), repo: repo.into() }
    }

    #[test]
    fn parses_supported_urls() {
        assert_eq!(
            parse_target("https://github.com/rockbox37/cheetah").unwrap(),
            readme("rockbox37", "cheetah")
        );
        assert_eq!(parse_target("https://www.github.com/o/r/").unwrap(), readme("o", "r"));
        assert_eq!(parse_target("https://github.com/o/r.git").unwrap(), readme("o", "r"));
        assert_eq!(
            parse_target("https://github.com/o/r/blob/main/src/lib.rs").unwrap(),
            Target::File {
                owner: "o".into(),
                repo: "r".into(),
                git_ref: "main".into(),
                path: vec!["src".into(), "lib.rs".into()]
            }
        );
        assert_eq!(
            parse_target("https://github.com/o/r/issues/5").unwrap(),
            Target::Issue { owner: "o".into(), repo: "r".into(), number: 5 }
        );
        assert_eq!(
            parse_target("https://github.com/o/r/pull/12").unwrap(),
            Target::Issue { owner: "o".into(), repo: "r".into(), number: 12 }
        );
    }

    #[test]
    fn decodes_percent_encoded_paths() {
        match parse_target("https://github.com/o/r/blob/main/docs/My%20Doc%C3%A9.md").unwrap() {
            Target::File { path, .. } => {
                assert_eq!(path, vec!["docs".to_string(), "My Docé.md".to_string()])
            }
            other => panic!("unexpected target {other:?}"),
        }
    }

    #[test]
    fn rejects_everything_else() {
        for bad in [
            "http://github.com/o/r",
            "https://github.com.evil.example/o/r",
            "https://api.github.com/repos/o/r",
            "https://gist.github.com/o/abc",
            "https://github.com/",
            "https://github.com/o",
            "https://github.com/o/r/tree/main/docs",
            "https://github.com/o/r/issues",
            "https://github.com/o/r/issues/abc",
            "https://github.com/o/r/issues/5/extra",
            "https://github.com/o/r/issues/99999999999999999999999",
            "https://github.com/o/r/blob/main",
            "https://github.com/o/r/blob/main/a%2Fb",
            "https://github.com/o/r/blob/main/a%5Cb",
            "https://github.com/o/r/blob/main/a%00b",
            "https://github.com/o/r/blob/main/%FF.md",
            "https://github.com/o/r/actions/runs/1",
            "https://github.com/o%2Fx/r",
            "not a url",
        ] {
            assert!(parse_target(bad).is_err(), "{bad} should be rejected");
        }
    }

    #[test]
    fn dot_segments_are_normalised_by_the_parser_not_forwarded() {
        // `..` never reaches the API URL: the URL parser resolves it first.
        match parse_target("https://github.com/o/r/blob/main/../etc/passwd").unwrap() {
            Target::File { git_ref, path, .. } => {
                assert_eq!(git_ref, "etc");
                assert_eq!(path, vec!["passwd".to_string()]);
            }
            other => panic!("unexpected target {other:?}"),
        }
    }

    #[test]
    fn builds_api_urls_with_encoding() {
        let u = build_url(
            API_BASE,
            &["repos", "o", "r", "contents", "my dir", "a.rs"],
            &[("ref", "feat x")],
        )
        .unwrap();
        assert_eq!(
            u.as_str(),
            "https://api.github.com/repos/o/r/contents/my%20dir/a.rs?ref=feat+x"
        );
    }

    #[test]
    fn fence_outgrows_content() {
        assert_eq!(fenced("a"), "```\na\n```");
        assert_eq!(fenced("x ``` y"), "````\nx ``` y\n````");
    }

    #[test]
    fn titles_are_one_line() {
        assert_eq!(one_line("a\nb\r\tc"), "a b  c");
    }

    #[test]
    fn renders_issue_thread() {
        let issue = serde_json::json!({
            "number": 5, "title": "Bug", "state": "open", "body": "It breaks.",
            "user": {"login": "alice"}, "labels": [{"name": "bug"}, {"name": "p1"}]
        });
        let comments = serde_json::json!([{"user": {"login": "bob"}, "created_at": "2026-01-01T00:00:00Z", "body": "Same here."}]);
        let md = render_issue(&issue, &comments);
        assert!(md.starts_with("# Bug (#5)"));
        assert!(md.contains("issue · open · opened by @alice · labels: bug, p1"));
        assert!(md.contains("It breaks."));
        assert!(md.contains("### @bob · 2026-01-01T00:00:00Z"));
        assert!(md.contains("Same here."));
        assert!(!md.contains("omitted"));
    }

    #[test]
    fn full_comment_page_is_marked_truncated() {
        let issue = serde_json::json!({"number": 1, "title": "t", "state": "open", "body": null, "user": null});
        let comments = Value::Array(
            (0..COMMENTS_PER_PAGE)
                .map(|_| serde_json::json!({"user": null, "created_at": "d", "body": "x"}))
                .collect(),
        );
        assert!(render_issue(&issue, &comments).contains("later ones are omitted"));
    }

    fn headers(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
        let mut h = HeaderMap::new();
        for (k, v) in pairs {
            h.insert(*k, HeaderValue::from_static(v));
        }
        h
    }

    #[test]
    fn cooldown_only_for_real_rate_limits() {
        let now = 1_000_000;
        assert_eq!(cooldown_until(200, &headers(&[]), now), None);
        assert_eq!(cooldown_until(404, &headers(&[]), now), None);
        // A bare 403 is a blocked or private repo, not a rate limit.
        assert_eq!(cooldown_until(403, &headers(&[]), now), None);
        assert_eq!(
            cooldown_until(403, &headers(&[("x-ratelimit-remaining", "12")]), now),
            None
        );
        assert_eq!(
            cooldown_until(
                403,
                &headers(&[("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "1000600")]),
                now
            ),
            Some(1_000_600)
        );
        assert_eq!(
            cooldown_until(429, &headers(&[("retry-after", "30")]), now),
            Some(1_000_030)
        );
        assert_eq!(cooldown_until(429, &headers(&[]), now), Some(1_000_060));
        // Never longer than the cap, never in the past.
        assert_eq!(
            cooldown_until(429, &headers(&[("retry-after", "999999")]), now),
            Some(now + MAX_COOLDOWN_SECS)
        );
        assert_eq!(
            cooldown_until(
                403,
                &headers(&[("x-ratelimit-remaining", "0"), ("x-ratelimit-reset", "5")]),
                now
            ),
            Some(now + 1)
        );
    }

    type Seen = Arc<Mutex<Vec<String>>>;

    /// Serves canned `(status, body)` replies keyed by request path prefix and
    /// records each raw request so tests can inspect headers.
    async fn fake_api(routes: Vec<(&'static str, u16, &'static [u8])>) -> (String, Seen) {
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
                    seen.lock().unwrap().push(req);
                    let (status, body) = routes
                        .iter()
                        .find(|(p, _, _)| path.starts_with(p))
                        .map(|(_, s, b)| (*s, *b))
                        .unwrap_or((404, b"not found"));
                    let head = format!(
                        "HTTP/1.1 {status} X\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    let _ = sock.write_all(head.as_bytes()).await;
                    let _ = sock.write_all(body).await;
                });
            }
        });
        (base, seen)
    }

    fn cfg() -> FetchConfig {
        FetchConfig::default()
    }

    fn with_token() -> FetchConfig {
        FetchConfig {
            github_token: Some(Secret::new("ghp_secret")),
            ..FetchConfig::default()
        }
    }

    #[tokio::test]
    async fn readme_round_trip() {
        let (base, seen) = fake_api(vec![("/repos/o/r/readme", 200, b"# Hello\n")]).await;
        let page = extract_with_base(&base, &Client::new(), "https://github.com/o/r", &cfg())
            .await
            .unwrap();
        assert_eq!(page.title.as_deref(), Some("o/r"));
        assert_eq!(page.markdown, "# Hello\n");
        let req = seen.lock().unwrap()[0].to_ascii_lowercase();
        assert!(req.contains("accept: application/vnd.github.raw+json"));
        assert!(req.contains("user-agent:"));
    }

    #[tokio::test]
    async fn file_is_fenced_unless_markdown() {
        let (base, _) = fake_api(vec![
            ("/repos/o/r/contents/a.rs", 200, b"fn main() {}"),
            ("/repos/o/r/contents/NOTES.md", 200, b"# Notes"),
            ("/repos/o/r/contents/My%20Doc.md", 200, b"spaced"),
        ])
        .await;
        let c = Client::new();
        let rs = extract_with_base(&base, &c, "https://github.com/o/r/blob/main/a.rs", &cfg())
            .await
            .unwrap();
        assert_eq!(rs.markdown, "```\nfn main() {}\n```");
        let md = extract_with_base(&base, &c, "https://github.com/o/r/blob/main/NOTES.md", &cfg())
            .await
            .unwrap();
        assert_eq!(md.markdown, "# Notes");
        let sp = extract_with_base(&base, &c, "https://github.com/o/r/blob/main/My%20Doc.md", &cfg())
            .await
            .unwrap();
        assert_eq!(sp.markdown, "spaced");
    }

    #[tokio::test]
    async fn issue_thread_round_trip_keeps_only_rendered_text() {
        let (base, _) = fake_api(vec![
            (
                "/repos/o/r/issues/5/comments",
                200,
                br#"[{"user":{"login":"bob"},"created_at":"d","body":"hi"}]"#,
            ),
            (
                "/repos/o/r/issues/5",
                200,
                br#"{"number":5,"title":"Bug","state":"open","body":"b","user":{"login":"a","email":"a@example.com"},"labels":[]}"#,
            ),
        ])
        .await;
        let page = extract_with_base(&base, &Client::new(), "https://github.com/o/r/issues/5", &cfg())
            .await
            .unwrap();
        assert_eq!(page.title.as_deref(), Some("Bug (#5)"));
        assert!(page.markdown.contains("@bob"));
        assert!(
            !page.raw_html.contains("a@example.com"),
            "raw JSON fields must not be stored"
        );
    }

    #[tokio::test]
    async fn refusals_empty_binary_and_oversize_are_extractor_errors() {
        let (base, _) = fake_api(vec![
            ("/repos/o/limited/readme", 403, b"nope"),
            ("/repos/o/binary/contents/x.bin", 200, &[0xff, 0xfe, 0x00, 0x80]),
            ("/repos/o/empty/readme", 200, b"  \n"),
            ("/repos/o/big/readme", 200, b"0123456789"),
            ("/repos/o/missing/readme", 404, b"gone"),
        ])
        .await;
        let c = Client::new();
        for url in [
            "https://github.com/o/limited",
            "https://github.com/o/missing",
            "https://github.com/o/empty",
            "https://github.com/o/binary/blob/main/x.bin",
        ] {
            let err = extract_with_base(&base, &c, url, &cfg()).await.unwrap_err();
            assert!(matches!(err, Error::Extractor(_)), "{url}: {err:?}");
        }
        let small = FetchConfig { max_body_size: 4, ..FetchConfig::default() };
        let err = extract_with_base(&base, &c, "https://github.com/o/big", &small)
            .await
            .unwrap_err();
        assert!(matches!(err, Error::BodyTooLarge { .. }));
    }

    #[tokio::test]
    async fn with_a_token_only_public_repos_are_read() {
        let (base, seen) = fake_api(vec![
            ("/repos/o/pub/readme", 200, b"public readme"),
            ("/repos/o/pub", 200, br#"{"private":false}"#),
            ("/repos/o/priv/readme", 200, b"SECRET"),
            ("/repos/o/priv", 200, br#"{"private":true}"#),
            ("/repos/o/odd", 200, br#"{}"#),
        ])
        .await;
        let c = Client::new();
        let ok = extract_with_base(&base, &c, "https://github.com/o/pub", &with_token())
            .await
            .unwrap();
        assert_eq!(ok.markdown, "public readme");
        for url in ["https://github.com/o/priv", "https://github.com/o/odd"] {
            let err = extract_with_base(&base, &c, url, &with_token()).await.unwrap_err();
            assert!(matches!(err, Error::Extractor(_)), "{url}: {err:?}");
        }
        assert!(
            !seen.lock().unwrap().iter().any(|r| r.contains("/priv/readme")),
            "private repo content must never be requested"
        );
    }

    #[tokio::test]
    async fn token_is_not_sent_to_non_api_hosts() {
        let (base, seen) = fake_api(vec![
            ("/repos/o/r/readme", 200, b"x"),
            ("/repos/o/r", 200, br#"{"private":false}"#),
        ])
        .await;
        extract_with_base(&base, &Client::new(), "https://github.com/o/r", &with_token())
            .await
            .unwrap();
        for req in seen.lock().unwrap().iter() {
            assert!(
                !req.to_ascii_lowercase().contains("authorization"),
                "token leaked to a non-api.github.com host: {req}"
            );
        }
    }

    #[test]
    fn secret_debug_is_redacted() {
        assert!(!format!("{:?}", with_token()).contains("ghp_secret"));
    }
}
