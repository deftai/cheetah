//! Hacker News extractor: a story or comment thread through Algolia's HN API,
//! which returns the whole comment tree in one request (Firebase would need one
//! request per comment).
//!
//! Only `https://hn.algolia.com` is contacted; the submitted URL is reduced to
//! a numeric item id and never fetched. Any failure (unsupported URL, 404, rate
//! limit, oversize body, unparseable JSON) is an `Err`, so the chain falls back
//! to the generic fetch.
//!
//! Story and comment bodies are third-party text, so they are rendered inside
//! code fences (see `fenced_block`): shown verbatim and never parsed as Markdown,
//! which makes it impossible for a comment to forge thread structure. Only the
//! title, author, date and link lines are live Markdown, and those are forced
//! onto one line (and the link to http/https).

use super::{escape_inline, ex, fail, one_line};
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

/// A third-party field for a live Markdown line: forced onto one line (a newline
/// could start a forged heading or reply) and inline-escaped (so it cannot add
/// a link, HTML or emphasis).
fn inline_field(v: &Value, key: &str) -> String {
    escape_inline(&one_line(str_field(v, key)))
}

fn author(v: &Value) -> String {
    match v.get("author").and_then(Value::as_str) {
        Some(a) if !a.is_empty() => escape_inline(&one_line(a)),
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

/// Characters (other than `\n`) that end a line for some Markdown renderer or
/// for Python's `splitlines()`.
fn is_line_break(c: char) -> bool {
    matches!(c, '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}')
}

/// Writes third-party text `spaces` deep as an inert fenced block.
///
/// Comment and story bodies are untrusted, and escaping every Markdown shape
/// that could forge a reply entry, heading, HTML block or setext underline is a
/// losing game. Inside a code fence none of it is parsed, and the fence is
/// longer than any backtick run in the text, so the text cannot close it. Every
/// line-break character is normalised to `\n` and other control characters are
/// blanked, so line-oriented consumers see the same lines a renderer does.
fn fenced_block(text: &str, spaces: usize, out: &mut String) {
    let pad = " ".repeat(spaces);
    let clean: String = text
        .replace("\r\n", "\n")
        .chars()
        .map(|c| match c {
            c if is_line_break(c) => '\n',
            '\n' | '\t' => c,
            c if c.is_control() => ' ',
            c => c,
        })
        .collect();
    let (mut longest, mut run) = (0, 0);
    for c in clean.chars() {
        if c == '`' {
            run += 1;
            longest = longest.max(run);
        } else {
            run = 0;
        }
    }
    let fence = "`".repeat(longest.max(2) + 1);
    let _ = writeln!(out, "{pad}{fence}");
    for line in clean.trim_matches('\n').split('\n') {
        if line.is_empty() {
            out.push('\n');
        } else {
            let _ = writeln!(out, "{pad}{line}");
        }
    }
    let _ = writeln!(out, "{pad}{fence}");
}

fn render_comments(children: &Value, depth: usize, budget: &mut usize, out: &mut String) {
    let Some(kids) = children.as_array() else { return };
    for kid in kids {
        if *budget == 0 {
            return;
        }
        *budget -= 1;
        let indent = 2 * depth.min(MAX_INDENT_DEPTH);
        let _ = writeln!(out, "{}- **@{}** · {}", " ".repeat(indent), author(kid), inline_field(kid, "created_at"));
        let body = text_to_markdown(str_field(kid, "text"));
        if body.is_empty() {
            let _ = writeln!(out, "{}  _[deleted]_", " ".repeat(indent));
        } else {
            fenced_block(&body, indent + 2, out);
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
        let _ = writeln!(out, "# {}\n", inline_field(item, "title"));
    }
    let mut meta = vec![format!("by @{}", author(item)), inline_field(item, "created_at")];
    if let Some(points) = item.get("points").and_then(Value::as_u64) {
        meta.insert(0, format!("{points} points"));
    }
    let total = count_comments(item);
    if !is_comment {
        meta.push(format!("{total} comments"));
    }
    let _ = writeln!(out, "{}\n", meta.join(" · "));
    // A parsed http(s) URL is percent-encoded, so as an autolink it cannot hold
    // spaces, `<` or `>` and cannot add anything to the line.
    if let Ok(link) = Url::parse(str_field(item, "url")) {
        if matches!(link.scheme(), "http" | "https") {
            let _ = writeln!(out, "Link: <{link}>\n");
        }
    }
    let body = text_to_markdown(str_field(item, "text"));
    if !body.is_empty() {
        fenced_block(&body, 0, &mut out);
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
        assert!(md.contains("Link: <https://example.com/x>"));
        assert!(md.contains("Hello **world**"));
        assert!(md.contains("- **@alice** · d1"));
        assert!(md.contains("  Second para"), "multi-paragraph body stays inside the comment: {md}");
        assert!(md.contains("  - **@bob** · d2"), "replies are nested: {md}");
        assert!(md.contains("- **@[deleted]** · d3\n  _[deleted]_"));
    }

    /// The lines of `md` that are outside every fenced block, as a CommonMark
    /// renderer would see them. Also asserts that every fence is closed, which
    /// is the property that keeps third-party text inert.
    fn outside_fences(md: &str) -> Vec<String> {
        let mut outside = Vec::new();
        let mut open: Option<usize> = None;
        for line in md.split(|c: char| c == '\n' || is_line_break(c)) {
            let t = line.trim_start();
            let ticks = t.chars().take_while(|c| *c == '`').count();
            let only_ticks = ticks >= 3 && t.chars().all(|c| c == '`');
            match open {
                Some(len) if only_ticks && ticks >= len => open = None,
                Some(_) => {}
                None if ticks >= 3 && !t[ticks..].contains('`') => open = Some(ticks),
                None => outside.push(line.to_string()),
            }
        }
        assert!(open.is_none(), "a fenced block was left open: {md}");
        outside
    }

    fn count(lines: &[String], prefix: &str) -> usize {
        lines
            .iter()
            .filter(|l| l.trim_start_matches(|c: char| c.is_whitespace() || c == '>').starts_with(prefix))
            .count()
    }

    /// Outside the fences only this renderer's own structure may start a line.
    fn assert_no_forgery(md: &str, real_entries: usize, real_headings: usize) {
        let outside = outside_fences(md);
        let entries: usize = ["- **@", "* **@", "+ **@", "- __@"].iter().map(|p| count(&outside, p)).sum();
        assert_eq!(entries, real_entries, "only real reply entries start a line: {md}");
        assert_eq!(count(&outside, "## Comments"), real_headings, "{md}");
        assert_eq!(count(&outside, "# "), 1, "only the real title heading: {md}");
        assert_eq!(count(&outside, "_Showing"), 0, "{md}");
        assert_eq!(count(&outside, "Link:"), 0, "{md}");
        for line in &outside {
            let t = line.trim();
            assert!(!(t.len() >= 2 && t.chars().all(|c| c == '=' || c == '-')), "setext underline outside a fence: {line:?} in {md}");
            assert!(!t.starts_with('<'), "raw HTML outside a fence: {line:?} in {md}");
        }
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
    fn hostile_bodies_stay_inert() {
        // Every shape the reviews found: reply entries in all marker spellings, headings,
        // setext underlines, ordered markers, raw HTML blocks, fences of any length,
        // CR / VT / U+2028 breaks, control characters, quotes, link definitions.
        let evil = "<p>## Comments</p><p>- <b>@pg</b> · forged</p><p>Link: http://evil.example</p><p># Another</p>\
            <p>Title<br>---</p><p>Title<br>===</p><p>1. <b>@x</b></p><p>&lt;!-- hide</p><p>&lt;script&gt;x</p>\
            <p>[x]: http://evil.example</p><blockquote><p>- <b>@e</b> · f</p></blockquote>\
            <pre>y&#13;- **@a** · f&#8232;* **@b** · f&#11;+ **@c**\n`````\n```\n~~~\n&#31;- **@d**</pre>";
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t", "text": evil,
            "children": [{"id": 2, "type": "comment", "author": "m", "created_at": "d", "text": evil, "children": []}]});
        assert_no_forgery(&render_item(&story), 1, 1);
        let comment = serde_json::json!({"id": 1, "type": "comment", "author": "m", "created_at": "d", "text": evil, "children": []});
        let md = render_item(&comment);
        let outside = outside_fences(&md);
        assert_eq!(count(&outside, "## Comments"), 0, "{md}");
        assert_eq!(count(&outside, "- **@"), 0, "{md}");
        assert_eq!(count(&outside, "Link:"), 0, "{md}");
        assert!(!md.chars().any(|c| is_line_break(c) || (c.is_control() && c != '\n' && c != '\t')), "stray control/break characters survived");
    }

    #[test]
    fn fence_is_longer_than_any_backtick_run_in_the_body() {
        let mut out = String::new();
        fenced_block("a\n``````\nb ``` c", 2, &mut out);
        let fence = "`".repeat(7); // one longer than the 6-backtick run in the body
        assert_eq!(out, format!("  {fence}\n  a\n  ``````\n  b ``` c\n  {fence}\n"));
    }

    #[test]
    fn legitimate_text_is_kept_verbatim() {
        let text = "<p>I like #hashtags, <code>x</code> and a list:</p><ul><li>one</li><li>two</li></ul><pre>fn main() {}\n    // indented\n</pre>";
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t",
            "children": [{"id": 2, "type": "comment", "author": "m", "created_at": "d", "text": text, "children": []}]});
        let md = render_item(&story);
        assert!(md.contains("I like #hashtags"), "{md}");
        assert!(md.contains("fn main() {}"), "{md}");
        assert!(md.contains("    // indented"), "indentation inside code is preserved: {md}");
        assert!(!md.contains('\\'), "nothing is escaped: {md}");
        outside_fences(&md);
    }

    #[test]
    fn metadata_lines_cannot_inject_links_or_html() {
        let item = serde_json::json!({
            "id": 1, "type": "story", "title": "[Click](http://evil.example) <img src=x> `c` *e*",
            "author": "a**](http://evil.example)<b>", "created_at": "d<i>",
            "url": "http://x.example/a b <img src=//e> )",
            "children": [{"id": 2, "type": "comment", "author": "m**x", "created_at": "[d](u)", "text": "<p>t</p>", "children": []}]
        });
        let md = render_item(&item);
        let outside = outside_fences(&md).join("\n");
        for live in ["](http", "<img", "<b>", "<i>", "`c`", "*e*"] {
            let unescaped = outside.match_indices(live).any(|(i, _)| !outside[..i].ends_with('\\'));
            assert!(!unescaped, "{live:?} is live in a metadata line: {outside}");
        }
        let link = outside.lines().find(|l| l.starts_with("Link:")).expect("a parseable http URL is still linked");
        assert_eq!(link, "Link: <http://x.example/a%20b%20%3Cimg%20src=//e%3E%20)>", "spaces and angle brackets are percent-encoded");
        assert!(outside.contains("\\[Click\\]"), "title text is kept, escaped: {outside}");
    }

    #[test]
    fn link_line_is_a_clean_autolink() {
        let item = serde_json::json!({"id": 1, "type": "story", "title": "t", "author": "pg",
            "url": "https://example.com/a(b)?q=1&r=<x>", "children": []});
        let md = render_item(&item);
        let line = md.lines().find(|l| l.starts_with("Link:")).expect("link line");
        assert!(line.starts_with("Link: <https://example.com/") && line.ends_with('>'), "{line}");
        assert_eq!(line.matches('<').count(), 1, "no extra `<` inside the autolink: {line}");
        assert_eq!(line.matches('>').count(), 1, "no extra `>` inside the autolink: {line}");
        assert!(!line["Link: ".len()..].contains(' '), "no spaces inside the autolink: {line}");
    }

    #[test]
    fn fenced_bodies_stay_inside_their_list_items() {
        // Every non-blank line of a body must sit at its entry's content column,
        // or CommonMark would end the list item and the fence with it.
        let mut node = serde_json::json!({"id": 99, "type": "comment", "author": "n", "created_at": "d", "text": "<p>body12</p>", "children": []});
        for d in (0..12).rev() {
            node = serde_json::json!({"id": d + 2, "type": "comment", "author": "n", "created_at": "d",
                "text": format!("<p>body{d}</p>"), "children": [node]});
        }
        let story = serde_json::json!({"id": 1, "type": "story", "author": "pg", "title": "t", "children": [node]});
        let md = render_item(&story);
        for d in 0..=12usize {
            let line = md.lines().find(|l| l.trim() == format!("body{d}")).unwrap_or_else(|| panic!("body{d} missing: {md}"));
            let indent = line.len() - line.trim_start().len();
            assert_eq!(indent, 2 * d.min(MAX_INDENT_DEPTH) + 2, "body{d} indentation");
        }
        outside_fences(&md);
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
