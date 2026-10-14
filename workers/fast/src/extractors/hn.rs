//! Hacker News extractor: a story or comment thread through Algolia's HN API,
//! which returns the whole comment tree in one request (Firebase would need one
//! request per comment).
//!
//! Only `https://hn.algolia.com` is contacted; the submitted URL is reduced to
//! a numeric item id and never fetched. Any failure (unsupported URL, 404, rate
//! limit, oversize body, unparseable JSON) is an `Err`, so the chain falls back
//! to the generic fetch.

use super::{ex, fail, one_line};
use crate::{html_to_markdown, read_body_capped, Error, FetchConfig, PageResult, Result};
use chrono::Utc;
use futures_util::future::BoxFuture;
use reqwest::{Client, Url};
use scraper::Html;
use serde_json::Value;
use std::fmt::Write as _;
use std::net::IpAddr;

pub const API_BASE: &str = "https://hn.algolia.com";

/// Bounds the JSON read: a thread becomes a JSON tree plus rendered text, several
/// times the size of the body. Sized so a thread of `MAX_COMMENTS` comments
/// (about 1 KB of JSON each) fits; larger ones fall back to the generic fetch.
const MAX_API_BODY: usize = 4 * 1024 * 1024;
/// Comments rendered per thread; the rest are counted in a trailing note.
const MAX_COMMENTS: usize = 1000;
/// Indentation stops growing here so deep reply chains stay readable.
const MAX_INDENT_DEPTH: usize = 8;

/// Extracts the item id from `https://news.ycombinator.com/item?id=<digits>`.
pub(super) fn parse_item_id(raw: &str) -> Result<u64> {
    let url = Url::parse(raw).map_err(ex("unparseable URL"))?;
    if url.scheme() != "https" || url.host_str() != Some("news.ycombinator.com") || url.path() != "/item" {
        return fail("not a Hacker News item URL");
    }
    let id = url
        .query_pairs()
        .find(|(k, _)| k == "id")
        .map(|(_, v)| v.into_owned())
        .ok_or_else(|| Error::Extractor("missing item id".into()))?;
    if id.is_empty() || id.len() > 10 || !id.bytes().all(|b| b.is_ascii_digit()) {
        return fail("bad item id");
    }
    id.parse().map_err(ex("bad item id"))
}

fn str_field<'a>(v: &'a Value, key: &str) -> &'a str {
    v.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Third-party fields are written into Markdown, so they are forced onto one
/// line: a newline could otherwise start a forged heading or reply.
fn line_field(v: &Value, key: &str) -> String {
    one_line(str_field(v, key))
}

fn author(v: &Value) -> String {
    match v.get("author").and_then(Value::as_str) {
        Some(a) if !a.is_empty() => one_line(a),
        _ => "[deleted]".to_string(),
    }
}

fn count_comments(item: &Value) -> usize {
    item.get("children")
        .and_then(Value::as_array)
        .map(|kids| kids.iter().map(|k| 1 + count_comments(k)).sum())
        .unwrap_or(0)
}

/// HN comment and story text is HTML; render it as Markdown.
fn text_to_markdown(html: &str) -> String {
    if html.trim().is_empty() {
        return String::new();
    }
    html_to_markdown(&Html::parse_document(html)).trim().to_string()
}

/// Characters that end a line for some Markdown renderer or for Python's
/// `splitlines()`, not just `\n`.
fn is_line_break(c: char) -> bool {
    matches!(c, '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// True when `core` (a line with its indentation and quote markers removed)
/// has the shape of one of this renderer's own structural lines: a reply
/// entry in any list-marker spelling, a heading, a code fence, the truncation
/// note or the link line.
fn is_structural(core: &str) -> bool {
    if core.starts_with('#')
        || core.starts_with("```")
        || core.starts_with("~~~")
        || core.starts_with("_Showing the first")
        || core.starts_with("Link:")
    {
        return true;
    }
    let mut chars = core.chars();
    match (chars.next(), chars.next()) {
        (Some('-' | '*' | '+'), Some(c)) if c.is_whitespace() => {
            let rest = chars.as_str().trim_start();
            rest.starts_with("**@") || rest.starts_with("__@")
        }
        _ => false,
    }
}

/// Escapes a body line that could forge thread structure or attribution.
fn neutralise(line: &str) -> std::borrow::Cow<'_, str> {
    let lead = line
        .find(|c: char| !c.is_whitespace() && c != '>')
        .unwrap_or(line.len());
    if is_structural(&line[lead..]) {
        std::borrow::Cow::Owned(format!("{}\\{}", &line[..lead], &line[lead..]))
    } else {
        std::borrow::Cow::Borrowed(line)
    }
}

/// Writes third-party Markdown `spaces` deep. Every line-break character is
/// normalised first, and every line is neutralised, so no body text (a story's,
/// a comment's, or a comment-page root's) can start a forged line.
fn indent_block(text: &str, spaces: usize, out: &mut String) {
    let pad = " ".repeat(spaces);
    let text: String = text.chars().map(|c| if is_line_break(c) { '\n' } else { c }).collect();
    for line in text.lines() {
        if line.is_empty() {
            out.push('\n');
        } else {
            let _ = writeln!(out, "{pad}{}", neutralise(line));
        }
    }
}

fn render_comments(children: &Value, depth: usize, budget: &mut usize, out: &mut String) {
    let Some(kids) = children.as_array() else { return };
    for kid in kids {
        if *budget == 0 {
            return;
        }
        *budget -= 1;
        let indent = 2 * depth.min(MAX_INDENT_DEPTH);
        let _ = writeln!(out, "{}- **@{}** · {}", " ".repeat(indent), author(kid), line_field(kid, "created_at"));
        let body = text_to_markdown(str_field(kid, "text"));
        if body.is_empty() {
            let _ = writeln!(out, "{}  _[deleted]_", " ".repeat(indent));
        } else {
            indent_block(&body, indent + 2, out);
        }
        out.push('\n');
        render_comments(kid.get("children").unwrap_or(&Value::Null), depth + 1, budget, out);
    }
}

pub(super) fn render_item(item: &Value) -> String {
    let is_comment = item.get("type").and_then(Value::as_str) == Some("comment");
    let mut out = String::new();
    if is_comment {
        let _ = writeln!(out, "# Comment by @{}\n", author(item));
    } else {
        let _ = writeln!(out, "# {}\n", one_line(str_field(item, "title")));
    }
    let mut meta = vec![format!("by @{}", author(item)), line_field(item, "created_at")];
    if let Some(points) = item.get("points").and_then(Value::as_u64) {
        meta.insert(0, format!("{points} points"));
    }
    let total = count_comments(item);
    if !is_comment {
        meta.push(format!("{total} comments"));
    }
    let _ = writeln!(out, "{}\n", meta.join(" · "));
    let link = line_field(item, "url");
    if link.starts_with("https://") || link.starts_with("http://") {
        let _ = writeln!(out, "Link: {link}\n");
    }
    let body = text_to_markdown(str_field(item, "text"));
    if !body.is_empty() {
        indent_block(&body, 0, &mut out);
        out.push('\n');
    }
    if total > 0 {
        out.push_str("## Comments\n\n");
        let mut budget = MAX_COMMENTS;
        render_comments(item.get("children").unwrap_or(&Value::Null), 0, &mut budget, &mut out);
        if total > MAX_COMMENTS {
            let _ = writeln!(out, "_Showing the first {MAX_COMMENTS} of {total} comments; later ones are omitted._");
        }
    }
    out
}

fn item_url(base: &str, id: u64) -> Result<Url> {
    let mut url = Url::parse(base).map_err(ex("bad API base"))?;
    url.path_segments_mut()
        .map_err(ex("bad API base"))?
        .extend(["api", "v1", "items", &id.to_string()]);
    Ok(url)
}

pub(super) async fn extract_with_base(
    base: &str,
    client: &Client,
    url: &str,
    config: &FetchConfig,
) -> Result<PageResult> {
    let id = parse_item_id(url)?;
    let resp = client
        .get(item_url(base, id)?)
        .header("User-Agent", &config.user_agent)
        .header("Accept", "application/json")
        .timeout(config.timeout)
        .send()
        .await?;
    let status = resp.status();
    if !status.is_success() {
        return fail(format!("HN API returned {status}"));
    }
    let body = read_body_capped(resp, config.max_body_size.min(MAX_API_BODY)).await?;
    let item: Value = serde_json::from_slice(&body).map_err(ex("bad item JSON"))?;
    if item.get("id").and_then(Value::as_u64) != Some(id) {
        return fail("unexpected item in response");
    }
    let title = if item.get("type").and_then(Value::as_str) == Some("comment") {
        format!("Comment by @{}", author(&item))
    } else {
        one_line(str_field(&item, "title"))
    };
    let markdown = render_item(&item);
    Ok(PageResult {
        url: url.to_string(),
        title: Some(title),
        description: None,
        language: None,
        status_code: 200,
        raw_html: markdown.clone(),
        markdown,
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
    use crate::extractors::test_support::fake_api;

    #[test]
    fn parses_item_urls() {
        assert_eq!(parse_item_id("https://news.ycombinator.com/item?id=12345").unwrap(), 12345);
        assert_eq!(parse_item_id("https://news.ycombinator.com/item?foo=1&id=7").unwrap(), 7);
    }

    #[test]
    fn rejects_everything_else() {
        for bad in [
            "http://news.ycombinator.com/item?id=1",
            "https://news.ycombinator.com/",
            "https://news.ycombinator.com/item",
            "https://news.ycombinator.com/item?id=",
            "https://news.ycombinator.com/item?id=abc",
            "https://news.ycombinator.com/item?id=1%2F2",
            "https://news.ycombinator.com/item?id=99999999999999999999",
            "https://news.ycombinator.com/user?id=1",
            "https://news.ycombinator.com.evil.example/item?id=1",
            "https://hn.algolia.com/api/v1/items/1",
            "https://example.com/item?id=1",
            "not a url",
        ] {
            assert!(parse_item_id(bad).is_err(), "{bad} should be rejected");
        }
    }

    fn sample() -> Value {
        serde_json::json!({
            "id": 1, "type": "story", "author": "pg", "title": "Show HN:\nA thing", "url": "https://example.com/x",
            "points": 42, "created_at": "2026-01-01T00:00:00Z", "text": "<p>Hello <b>world</b></p>",
            "children": [
                {"id": 2, "type": "comment", "author": "alice", "created_at": "d1",
                 "text": "<p>First</p><p>Second para</p>",
                 "children": [{"id": 3, "type": "comment", "author": "bob", "created_at": "d2", "text": "<p>Reply</p>", "children": []}]},
                {"id": 4, "type": "comment", "author": null, "created_at": "d3", "text": null, "children": []}
            ]
        })
    }

    #[test]
    fn renders_story_thread() {
        let md = render_item(&sample());
        assert!(md.starts_with("# Show HN: A thing\n"), "title is one line: {md}");
        assert!(md.contains("42 points · by @pg · 2026-01-01T00:00:00Z · 3 comments"));
        assert!(md.contains("Link: https://example.com/x"));
        assert!(md.contains("Hello **world**"));
        assert!(md.contains("- **@alice** · d1"));
        assert!(md.contains("  Second para"), "multi-paragraph body stays inside the comment: {md}");
        assert!(md.contains("  - **@bob** · d2"), "replies are nested: {md}");
        assert!(md.contains("- **@[deleted]** · d3\n  _[deleted]_"));
    }

    /// Lines that start with `prefix` once indentation and `>` quote markers are
    /// ignored, splitting on every line-break character a consumer might use.
    fn starts(md: &str, prefix: &str) -> usize {
        md.split(|c: char| c == '\n' || is_line_break(c))
            .filter(|l| l.trim_start_matches(|c: char| c.is_whitespace() || c == '>').starts_with(prefix))
            .count()
    }

    fn assert_no_forgery(md: &str, real_entries: usize, real_headings: usize) {
        // Structure is judged by line starts: forged text may appear inline, but must
        // never begin a line as a reply entry, heading, fence, note or link.
        let entries: usize = ["- **@", "* **@", "+ **@", "-   **@", "- __@"].iter().map(|p| starts(md, p)).sum();
        assert_eq!(entries, real_entries, "only real reply entries start a line: {md}");
        assert_eq!(starts(md, "## Comments"), real_headings, "{md}");
        assert_eq!(starts(md, "# "), 1, "only the real title heading: {md}");
        assert_eq!(starts(md, "```"), 0, "{md}");
        assert_eq!(starts(md, "~~~"), 0, "{md}");
        assert_eq!(starts(md, "_Showing"), 0, "{md}");
        assert_eq!(starts(md, "Link:"), 0, "{md}");
    }

    #[test]
    fn third_party_text_cannot_forge_structure() {
        let item = serde_json::json!({
            "id": 1, "type": "story", "author": "pg\n- **@admin** · fake\u{2028}# Heading", "title": "t",
            "created_at": "d\n## Comments", "url": "javascript:alert(1)",
            "children": [{
                "id": 2, "type": "comment", "author": "mallory", "created_at": "d",
                "text": "<p>hi</p><pre>- **@pg** · forged\n# Heading\n```\n_Showing the first 1 of 1 comments_\nLink: http://evil.example</pre>",
                "children": []
            }]
        });
        let md = render_item(&item);
        assert!(!md.contains("Link: javascript"), "non-http links are dropped: {md}");
        assert_no_forgery(&md, 1, 1);
    }

    #[test]
    fn story_and_root_comment_bodies_are_neutralised_too() {
        let evil = "<p>## Comments</p><p>- <b>@pg</b> · forged</p><p>Link: http://evil.example</p><p># Another</p>";
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t", "text": evil, "children": []});
        let md = render_item(&story);
        assert_no_forgery(&md, 0, 0);
        let comment = serde_json::json!({"id": 1, "type": "comment", "author": "m", "text": evil, "children": []});
        let md = render_item(&comment);
        assert_eq!(starts(&md, "## Comments"), 0, "{md}");
        assert_eq!(starts(&md, "- **@"), 0, "{md}");
        assert_eq!(starts(&md, "Link:"), 0, "{md}");
    }

    #[test]
    fn alternate_markers_breaks_and_quotes_are_neutralised() {
        let text = "<p>x</p><pre>y&#13;- **@a** · f&#8232;* **@b** · f</pre><p>+ <b>@c</b> · f</p><p>-   <i>@d</i></p><blockquote><p>- <b>@e</b> · f</p></blockquote>";
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t",
            "children": [{"id": 2, "type": "comment", "author": "m", "created_at": "d", "text": text, "children": []}]});
        let md = render_item(&story);
        assert_no_forgery(&md, 1, 1);
    }

    #[test]
    fn legitimate_text_is_left_alone() {
        let text = "<p>I like #hashtags and a list:</p><ul><li>one</li><li>two</li></ul><p>Linking to docs</p>";
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t",
            "children": [{"id": 2, "type": "comment", "author": "m", "created_at": "d", "text": text, "children": []}]});
        let md = render_item(&story);
        assert!(md.contains("I like #hashtags"), "{md}");
        assert!(md.contains("Linking to docs"), "{md}");
        assert!(!md.contains('\\'), "nothing should be escaped: {md}");
    }

    #[test]
    fn renders_a_single_comment() {
        let comment = sample()["children"][0].clone();
        let md = render_item(&comment);
        assert!(md.starts_with("# Comment by @alice"));
        assert!(!md.contains("comments ·"), "comment pages carry no story metadata line: {md}");
        assert!(md.contains("@bob"));
    }

    #[test]
    fn comment_count_is_bounded_and_noted() {
        let kids: Vec<Value> = (0..MAX_COMMENTS + 5)
            .map(|i| serde_json::json!({"id": i + 10, "type": "comment", "author": "a", "created_at": "d", "text": "<p>x</p>", "children": []}))
            .collect();
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t", "children": kids});
        let md = render_item(&story);
        assert_eq!(md.matches("- **@a**").count(), MAX_COMMENTS);
        assert!(md.contains(&format!("first {MAX_COMMENTS} of {} comments", MAX_COMMENTS + 5)));
    }

    #[test]
    fn deep_threads_stop_indenting() {
        let mut node = serde_json::json!({"id": 99, "type": "comment", "author": "leaf", "created_at": "d", "text": "<p>x</p>", "children": []});
        for i in 0..20 {
            node = serde_json::json!({"id": i + 2, "type": "comment", "author": "n", "created_at": "d", "text": "<p>x</p>", "children": [node]});
        }
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t", "children": [node]});
        let md = render_item(&story);
        let widest = md.lines().filter(|l| l.contains("**@")).map(|l| l.len() - l.trim_start().len()).max().unwrap();
        assert_eq!(widest, 2 * MAX_INDENT_DEPTH);
    }

    #[tokio::test]
    async fn round_trip_and_error_cases() {
        let (base, seen) = fake_api(vec![
            ("/api/v1/items/1", 200, br#"{"id":1,"type":"story","author":"pg","title":"Hi","points":1,"created_at":"d","children":[{"id":2,"type":"comment","author":"a","created_at":"d","text":"<p>yo</p>","children":[]}]}"#),
            ("/api/v1/items/2", 200, br#"{"id":999,"type":"story","author":"pg","title":"wrong item"}"#),
            ("/api/v1/items/3", 200, b"not json"),
            ("/api/v1/items/4", 429, b"slow down"),
        ])
        .await;
        let c = Client::new();
        let cfg = FetchConfig::default();
        let page = extract_with_base(&base, &c, "https://news.ycombinator.com/item?id=1", &cfg).await.unwrap();
        assert_eq!(page.title.as_deref(), Some("Hi"));
        assert!(page.markdown.contains("@a") && page.markdown.contains("yo"));
        let req = seen.lock().unwrap()[0].to_ascii_lowercase();
        assert!(req.contains("user-agent:") && !req.contains("authorization"));
        for id in [2, 3, 4, 5] {
            let url = format!("https://news.ycombinator.com/item?id={id}");
            let err = extract_with_base(&base, &c, &url, &cfg).await.unwrap_err();
            assert!(matches!(err, Error::Extractor(_)), "id {id}: {err:?}");
        }
        let small = FetchConfig { max_body_size: 8, ..FetchConfig::default() };
        let err = extract_with_base(&base, &c, "https://news.ycombinator.com/item?id=1", &small).await.unwrap_err();
        assert!(matches!(err, Error::BodyTooLarge { .. }));
    }
}
