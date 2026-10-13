//! GitHub extractor: repo README, file contents and issue/PR threads through
//! the REST API instead of scraping github.com HTML.
//!
//! Only `api.github.com` is ever contacted: the target URL is parsed into an
//! allow-listed request shape and its own host is never fetched. The optional
//! token is sent to that host only. Any failure (unsupported URL, 403/429 rate
//! limit, 404, oversize or binary content) surfaces as `Error::Extractor`, so
//! the chain falls back to the generic fetch.

use crate::{validate_url_ip, Error, FetchConfig, PageResult, Result};
use chrono::Utc;
use futures_util::future::BoxFuture;
use futures_util::StreamExt;
use reqwest::{Client, Url};
use serde_json::Value;
use std::net::IpAddr;

pub const API_BASE: &str = "https://api.github.com";
const API_HOST: &str = "api.github.com";
const ACCEPT_JSON: &str = "application/vnd.github+json";
const ACCEPT_RAW: &str = "application/vnd.github.raw+json";

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

/// Path and ref segments are re-encoded by the URL builder, so anything already
/// percent-encoded (or able to traverse) is refused rather than decoded.
fn valid_segment(s: &str) -> bool {
    !s.is_empty() && s != "." && s != ".." && !s.contains('%') && !s.contains('\\')
}

pub(super) fn parse_target(raw: &str) -> Result<Target> {
    let url = match Url::parse(raw) {
        Ok(u) => u,
        Err(_) => return fail("unparseable URL"),
    };
    if url.scheme() != "https" || !matches!(url.host_str(), Some("github.com" | "www.github.com")) {
        return fail("not a github.com URL");
    }
    let segs: Vec<&str> = url
        .path_segments()
        .map(|s| s.filter(|p| !p.is_empty()).collect())
        .unwrap_or_default();
    if segs.len() < 2 || !valid_name(segs[0]) || !valid_name(segs[1]) {
        return fail("not a repository URL");
    }
    let (owner, repo) = (segs[0].to_string(), segs[1].to_string());
    match &segs[2..] {
        [] | ["tree", ..] => Ok(Target::Readme { owner, repo }),
        ["blob", git_ref, path @ ..] if !path.is_empty() => {
            if !valid_segment(git_ref) || !path.iter().all(|p| valid_segment(p)) {
                return fail("unsupported file path");
            }
            Ok(Target::File {
                owner,
                repo,
                git_ref: (*git_ref).to_string(),
                path: path.iter().map(|p| p.to_string()).collect(),
            })
        }
        ["issues" | "pull", n] if !n.is_empty() && n.len() <= 10 && n.bytes().all(|b| b.is_ascii_digit()) => {
            match n.parse() {
                Ok(number) => Ok(Target::Issue { owner, repo, number }),
                Err(_) => fail("bad issue number"),
            }
        }
        _ => fail("unsupported GitHub URL"),
    }
}

fn build_url(base: &str, segments: &[&str], query: &[(&str, &str)]) -> Result<Url> {
    let mut url = match Url::parse(base) {
        Ok(u) => u,
        Err(_) => return fail("bad API base"),
    };
    match url.path_segments_mut() {
        Ok(mut s) => {
            s.extend(segments);
        }
        Err(_) => return fail("bad API base"),
    }
    if !query.is_empty() {
        url.query_pairs_mut().extend_pairs(query);
    }
    Ok(url)
}

/// Returns the body of a successful response, or an extractor error.
async fn api_get(
    client: &Client,
    url: Url,
    accept: &str,
    config: &FetchConfig,
    validate_dns: bool,
) -> Result<Vec<u8>> {
    if validate_dns {
        validate_url_ip(url.as_str()).await?;
    }
    let mut req = client
        .get(url.clone())
        .header("User-Agent", &config.user_agent)
        .header("Accept", accept)
        .header("X-GitHub-Api-Version", "2022-11-28")
        .timeout(config.timeout);
    if url.host_str() == Some(API_HOST) {
        if let Some(token) = &config.github_token {
            req = req.bearer_auth(token.expose());
        }
    }
    let resp = req.send().await?;
    let status = resp.status();
    if status.as_u16() == 403 || status.as_u16() == 429 {
        return fail(format!(
            "GitHub API refused the request ({status}); likely rate limited"
        ));
    }
    if !status.is_success() {
        return fail(format!("GitHub API returned {status}"));
    }
    if let Some(len) = resp.content_length() {
        if len > config.max_body_size as u64 {
            return Err(Error::BodyTooLarge {
                max_bytes: config.max_body_size,
            });
        }
    }
    let mut body = Vec::new();
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk?;
        if body.len() + chunk.len() > config.max_body_size {
            return Err(Error::BodyTooLarge {
                max_bytes: config.max_body_size,
            });
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

fn utf8(body: Vec<u8>) -> Result<String> {
    match String::from_utf8(body) {
        Ok(s) => Ok(s),
        Err(_) => fail("content is not UTF-8 text"),
    }
}

/// Wraps text in a code fence long enough that the text cannot close it.
fn fenced(text: &str) -> String {
    let (mut longest, mut run) = (0, 0);
    for c in text.chars() {
        if c == '`' {
            run += 1;
            longest = longest.max(run);
        } else {
            run = 0;
        }
    }
    let fence = "`".repeat(longest.max(2) + 1);
    format!("{fence}\n{text}\n{fence}")
}

fn str_field<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

fn login(v: &Value) -> &str {
    v.get("user")
        .and_then(|u| u.get("login"))
        .and_then(Value::as_str)
        .unwrap_or("ghost")
}

pub(super) fn render_issue(issue: &Value, comments: &Value) -> String {
    let number = issue.get("number").and_then(Value::as_u64).unwrap_or(0);
    let mut out = format!("# {} (#{number})\n\n", str_field(issue, "title"));
    let kind = if issue.get("pull_request").is_some() {
        "pull request"
    } else {
        "issue"
    };
    out.push_str(&format!(
        "{kind} · {} · opened by @{}",
        str_field(issue, "state"),
        login(issue)
    ));
    if let Some(labels) = issue.get("labels").and_then(Value::as_array) {
        let names: Vec<&str> = labels
            .iter()
            .filter_map(|l| l.get("name").and_then(Value::as_str))
            .collect();
        if !names.is_empty() {
            out.push_str(&format!(" · labels: {}", names.join(", ")));
        }
    }
    out.push_str("\n\n");
    out.push_str(str_field(issue, "body"));
    if let Some(list) = comments.as_array() {
        if !list.is_empty() {
            out.push_str("\n\n## Comments\n");
            for c in list {
                out.push_str(&format!(
                    "\n### @{} · {}\n\n{}\n",
                    login(c),
                    str_field(c, "created_at"),
                    str_field(c, "body")
                ));
            }
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
    let target = parse_target(url)?;
    // The DNS/private-IP check guards the real API host; tests point `base` at
    // a loopback fake and skip it.
    let validate_dns = base == API_BASE;
    let (title, markdown, raw) = match target {
        Target::Readme { owner, repo } => {
            let api = build_url(base, &["repos", &owner, &repo, "readme"], &[])?;
            let text = utf8(api_get(client, api, ACCEPT_RAW, config, validate_dns).await?)?;
            (format!("{owner}/{repo}"), text.clone(), text)
        }
        Target::File {
            owner,
            repo,
            git_ref,
            path,
        } => {
            let mut segs = vec!["repos", owner.as_str(), repo.as_str(), "contents"];
            segs.extend(path.iter().map(String::as_str));
            let api = build_url(base, &segs, &[("ref", &git_ref)])?;
            let text = utf8(api_get(client, api, ACCEPT_RAW, config, validate_dns).await?)?;
            let name = path.join("/");
            let md = if name.to_ascii_lowercase().ends_with(".md") {
                text.clone()
            } else {
                fenced(&text)
            };
            (format!("{owner}/{repo}: {name}"), md, text)
        }
        Target::Issue { owner, repo, number } => {
            let n = number.to_string();
            let issue_url = build_url(base, &["repos", &owner, &repo, "issues", &n], &[])?;
            let comments_url = build_url(
                base,
                &["repos", &owner, &repo, "issues", &n, "comments"],
                &[("per_page", "100")],
            )?;
            let issue_raw = api_get(client, issue_url, ACCEPT_JSON, config, validate_dns).await?;
            let comments_raw =
                api_get(client, comments_url, ACCEPT_JSON, config, validate_dns).await?;
            let issue: Value = match serde_json::from_slice(&issue_raw) {
                Ok(v) => v,
                Err(_) => return fail("bad issue JSON"),
            };
            let comments: Value = match serde_json::from_slice(&comments_raw) {
                Ok(v) => v,
                Err(_) => return fail("bad comments JSON"),
            };
            let md = render_issue(&issue, &comments);
            (
                format!("{} (#{number})", str_field(&issue, "title")),
                md,
                utf8(issue_raw)?,
            )
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
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn t(url: &str) -> Result<Target> {
        parse_target(url)
    }

    #[test]
    fn parses_supported_urls() {
        assert_eq!(
            t("https://github.com/rockbox37/cheetah").unwrap(),
            Target::Readme { owner: "rockbox37".into(), repo: "cheetah".into() }
        );
        assert_eq!(
            t("https://www.github.com/rockbox37/cheetah/tree/main/docs").unwrap(),
            Target::Readme { owner: "rockbox37".into(), repo: "cheetah".into() }
        );
        assert_eq!(
            t("https://github.com/o/r/blob/main/src/lib.rs").unwrap(),
            Target::File {
                owner: "o".into(),
                repo: "r".into(),
                git_ref: "main".into(),
                path: vec!["src".into(), "lib.rs".into()]
            }
        );
        assert_eq!(
            t("https://github.com/o/r/issues/5").unwrap(),
            Target::Issue { owner: "o".into(), repo: "r".into(), number: 5 }
        );
        assert_eq!(
            t("https://github.com/o/r/pull/12").unwrap(),
            Target::Issue { owner: "o".into(), repo: "r".into(), number: 12 }
        );
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
            "https://github.com/o/r/issues",
            "https://github.com/o/r/issues/abc",
            "https://github.com/o/r/issues/5/extra",
            "https://github.com/o/r/blob/main",
            "https://github.com/o/r/blob/main/a%2Fb",
            "https://github.com/o/r/actions/runs/1",
            "https://github.com/o%2Fx/r",
            "not a url",
        ] {
            assert!(t(bad).is_err(), "{bad} should be rejected");
        }
    }

    #[test]
    fn dot_segments_are_normalised_by_the_parser_not_forwarded() {
        // `..` never reaches the API URL: the URL parser resolves it first.
        match t("https://github.com/o/r/blob/main/../etc/passwd").unwrap() {
            Target::File { git_ref, path, .. } => {
                assert_eq!(git_ref, "etc");
                assert_eq!(path, vec!["passwd".to_string()]);
            }
            other => panic!("unexpected target {other:?}"),
        }
    }

    #[test]
    fn builds_api_urls_with_encoding() {
        let u = build_url(API_BASE, &["repos", "o", "r", "contents", "my dir", "a.rs"], &[("ref", "feat x")]).unwrap();
        assert_eq!(u.as_str(), "https://api.github.com/repos/o/r/contents/my%20dir/a.rs?ref=feat+x");
    }

    #[test]
    fn fence_outgrows_content() {
        assert_eq!(fenced("a"), "```\na\n```");
        assert_eq!(fenced("x ``` y"), "````\nx ``` y\n````");
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

    #[tokio::test]
    async fn readme_round_trip() {
        let (base, seen) = fake_api(vec![("/repos/o/r/readme", 200, b"# Hello\n")]).await;
        let page = extract_with_base(&base, &Client::new(), "https://github.com/o/r", &cfg()).await.unwrap();
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
        ])
        .await;
        let c = Client::new();
        let rs = extract_with_base(&base, &c, "https://github.com/o/r/blob/main/a.rs", &cfg()).await.unwrap();
        assert_eq!(rs.markdown, "```\nfn main() {}\n```");
        let md = extract_with_base(&base, &c, "https://github.com/o/r/blob/main/NOTES.md", &cfg()).await.unwrap();
        assert_eq!(md.markdown, "# Notes");
    }

    #[tokio::test]
    async fn issue_thread_round_trip() {
        let (base, _) = fake_api(vec![
            ("/repos/o/r/issues/5/comments", 200, br#"[{"user":{"login":"bob"},"created_at":"d","body":"hi"}]"#),
            ("/repos/o/r/issues/5", 200, br#"{"number":5,"title":"Bug","state":"open","body":"b","user":{"login":"a"},"labels":[]}"#),
        ])
        .await;
        let page = extract_with_base(&base, &Client::new(), "https://github.com/o/r/issues/5", &cfg()).await.unwrap();
        assert_eq!(page.title.as_deref(), Some("Bug (#5)"));
        assert!(page.markdown.contains("@bob"));
    }

    #[tokio::test]
    async fn rate_limit_not_found_binary_and_oversize_are_extractor_errors() {
        let (base, _) = fake_api(vec![
            ("/repos/o/limited/readme", 403, b"rate limit"),
            ("/repos/o/binary/contents/x.bin", 200, &[0xff, 0xfe, 0x00, 0x80]),
            ("/repos/o/big/readme", 200, b"0123456789"),
        ])
        .await;
        let c = Client::new();
        for url in [
            "https://github.com/o/limited",
            "https://github.com/o/missing",
            "https://github.com/o/binary/blob/main/x.bin",
        ] {
            let err = extract_with_base(&base, &c, url, &cfg()).await.unwrap_err();
            assert!(matches!(err, Error::Extractor(_)), "{url}: {err:?}");
        }
        let small = FetchConfig { max_body_size: 4, ..FetchConfig::default() };
        let err = extract_with_base(&base, &c, "https://github.com/o/big", &small).await.unwrap_err();
        assert!(matches!(err, Error::BodyTooLarge { .. }));
    }

    #[tokio::test]
    async fn token_is_not_sent_to_non_api_hosts() {
        let (base, seen) = fake_api(vec![("/repos/o/r/readme", 200, b"x")]).await;
        let with_token = FetchConfig { github_token: Some(Secret::new("ghp_secret")), ..FetchConfig::default() };
        extract_with_base(&base, &Client::new(), "https://github.com/o/r", &with_token).await.unwrap();
        let req = seen.lock().unwrap()[0].to_ascii_lowercase();
        assert!(!req.contains("authorization"), "token leaked to a non-api.github.com host: {req}");
    }

    #[test]
    fn secret_debug_is_redacted() {
        let cfg = FetchConfig { github_token: Some(Secret::new("ghp_secret")), ..FetchConfig::default() };
        assert!(!format!("{cfg:?}").contains("ghp_secret"));
    }
}
