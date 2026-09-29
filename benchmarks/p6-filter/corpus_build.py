"""Build the P6 labelled URL/request corpus (design §21, Gate E).

Labels are assigned here, by hand, before any ruleset is evaluated on the
corpus; the output is committed (URLs and labels only, no page bodies) to
``tests/fixtures/filtering/labeled_corpus.json``.

Two parts:

1. **Sampled** (``synthetic: false``): same-site links from the W691 HTML
   capture (``var/p5-corpus/capture-1``). Every W691 site is a piracy/content
   source, so a page of that site is labelled ``content`` — or
   ``navigation`` when its path is a listing/pagination/category page (the
   ``_NAV_MARKERS`` below, applied once while building). Plus the real
   third-party requests observed on those pages, labelled per host below.
2. **Hand-written** (``synthetic: true``): media, player, ad, tracker,
   redirect, social and adversarial URLs chosen to exercise the rules.
   Their hosts are real, public endpoints; their paths are illustrative.

Rebuild (needs the capture): ``env/bin/python benchmarks/p6-filter/corpus_build.py``
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "p5-extraction"))

import corpus  # noqa: E402

from crawler2.filtering.inputs import host_of, registrable_domain  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "filtering" / "labeled_corpus.json"
CAPTURE = ROOT / "var" / "p5-corpus" / "capture-1"
SAMPLED_CONTENT = 160
_NAV_MARKERS = ("/page/", "/category/", "/genre", "?page=", "/tag/", "/year/", "/browse", "/search")

# Real third-party requests seen on W691 pages, labelled per registrable domain.
REQUEST_LABELS = {
    "wpadmngr.com": "ad",
    "effectivecpmnetwork.com": "ad",
    "cloudflareinsights.com": "tracker",
    "googletagmanager.com": "tracker",
    "myimg.click": "content",  # poster image host
    "imgshare.info": "content",
    "catimages.org": "content",
    "tamilprinttv.com": "content",
    "googleusercontent.com": "content",
    "bp.blogspot.com": "content",
    "imgur.com": "content",
    "imagehosting.space": "content",
    "cloudflare.com": "unknown",  # cdnjs libraries
    "ajax.googleapis.com": "unknown",
    "jquery.com": "unknown",
    "bootstrapcdn.com": "unknown",
    "jsdelivr.net": "unknown",
    "fonts.googleapis.com": "unknown",
    "blogger.com": "unknown",
    "blogblog.com": "unknown",
    "gravatar.com": "unknown",
    "youtube.com": "player",  # oembed player iframes
}

P = "https://www.moviesda.farm/tamil-2026-movies/"  # a W691 seed page as first party

HAND: list[dict[str, object]] = []


def add(
    label: str, context: str, url: str, kind: str = "document", page: str | None = P, note: str = ""
) -> None:
    HAND.append(
        {
            "label": label,
            "context": context,
            "url": url,
            "resource_type": kind,
            "first_party": page,
            "synthetic": True,
            "note": note,
        }
    )


# media: files, manifests and download hosts (must never be blocked)
for url in [
    "https://dl1.hotshare.click/file/Leo-2023-Tamil-HD.mp4",
    "https://dl6.hotshare.click/d/8f2a/Jailer.mkv",
    "https://new1.movcloud.click/v/abc123/master.m3u8",
    "https://new3.movcloud.click/v/abc123/720p/seg-00012.ts",
    "https://photojin.baby/download/YE-Hqg5APHW",
    "https://cdn.moviesda.farm/uploads/2026/trailer.mp4",
    "https://media.tamilprint67.art/stream/1080/index.m3u8",
    "https://files.isaidub.love/dash/manifest.mpd",
    "https://video.twimg.com/ext_tw_video/1/pu/vid/720x1280/x.mp4",
    "https://vz-1a2b.b-cdn.net/5f1e/playlist.m3u8",
    "https://s3.amazonaws.com/some-bucket/movie.part1.mp4",
    "https://drive.usercontent.google.com/download?id=1AbC&export=download",
    "https://pixeldrain.com/api/file/AbCdEf",
    "https://gofile.io/d/AbC123",
    "https://www.mediafire.com/file/abc/Movie.mkv/file",
    "https://mega.nz/file/AbC#key",
    "https://1fichier.com/?abc123",
    "https://ddownload.com/abc123/Movie.mkv",
    "https://cdn.discordapp.com/attachments/1/2/movie.mp4",
    "https://videos.files.wordpress.com/abc/movie.mp4",
]:
    add("media", "link", url, note="media file, manifest or download host")
for url in [
    "https://new2.movcloud.click/v/abc/1080p/seg-1.ts",
    "https://vz-1a2b.b-cdn.net/5f1e/1080p/video.m3u8",
    "https://cdn.moviesda.farm/hls/leo/index.m3u8",
    "https://stream.tamilprint67.art/hls/seg-003.ts",
    "https://files.isaidub.love/v/track-02.m4s",
]:
    add("media", "request", url, "media", note="media sub-request of a player")

# player: embed hosts and player libraries
for url, kind in [
    ("https://www.youtube.com/embed/qQlr9-rF32A", "document"),
    ("https://streamtape.com/e/AbCdEf123/", "document"),
    ("https://dood.watch/e/abc123def", "document"),
    ("https://filemoon.sx/e/abc123def", "document"),
    ("https://mixdrop.ag/e/abc123", "document"),
    ("https://voe.sx/e/abc123", "document"),
    ("https://streamwish.to/e/abc123", "document"),
    ("https://vidhidepro.com/v/abc123", "document"),
    ("https://player.vimeo.com/video/123456", "document"),
    ("https://ok.ru/videoembed/123456", "document"),
    ("https://cdn.jwplayer.com/libraries/AbC123.js", "script"),
    ("https://ssl.p.jwpcdn.com/player/v/8.36.0/jwplayer.js", "script"),
    ("https://vjs.zencdn.net/8.10.0/video.min.js", "script"),
    ("https://cdn.plyr.io/3.7.8/plyr.js", "script"),
    ("https://cdn.jsdelivr.net/npm/hls.js@1.5.0/dist/hls.min.js", "script"),
    ("https://cdnjs.cloudflare.com/ajax/libs/video.js/8.10.0/video.min.js", "script"),
    ("https://content.jwplatform.com/players/AbC-123.html", "document"),
    ("https://www.dailymotion.com/embed/video/x8abc", "document"),
    ("https://embed.twitch.tv/?channel=x", "document"),
    ("https://player.twitch.tv/?video=1", "document"),
]:
    add("player", "request", url, kind, note="player / embed resource")

# ad: well-known ad endpoints and ad networks seen on piracy sites
for url, kind in [
    ("https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-1", "script"),
    ("https://securepubads.g.doubleclick.net/tag/js/gpt.js", "script"),
    ("https://ad.doubleclick.net/ddm/trackclk/N1.2/B3", "image"),
    ("https://www.googleadservices.com/pagead/conversion.js", "script"),
    ("https://c.adsco.re/", "script"),
    ("https://a.exoclick.com/tag_gen.js", "script"),
    ("https://syndication.exoclick.com/splash.php?idzone=1", "script"),
    ("https://c1.popads.net/pop.js", "script"),
    ("https://cdn.popcash.net/show.js", "script"),
    ("https://ad.a-ads.com/123456?size=728x90", "document"),
    ("https://s.pubmine.com/head.js", "script"),
    ("https://cdn.taboola.com/libtrc/site/loader.js", "script"),
    ("https://widgets.outbrain.com/outbrain.js", "script"),
    ("https://jsc.mgid.com/site/123.js", "script"),
    ("https://acdn.adnxs.com/ast/ast.js", "script"),
    ("https://ib.adnxs.com/ut/v3/prebid", "xhr"),
    ("https://js.wpadmngr.com/static/adManager.js", "script"),
    ("https://pl29482794.effectivecpmnetwork.com/e6/88/62/e688.js", "script"),
    ("https://www.highperformanceformat.com/abc/invoke.js", "script"),
    ("https://thubanoa.com/1?z=123", "script"),
    ("https://a.magsrv.com/ad-provider.js", "script"),
    ("https://static.surfe.pro/js/net.js", "script"),
    ("https://cdn.adpushup.com/123/adpushup.js", "script"),
    ("https://ads.pubmatic.com/AdServer/js/pwt/1/pwt.js", "script"),
    ("https://c.amazon-adsystem.com/aax2/apstag.js", "script"),
]:
    add("ad", "request", url, kind, note="ad network endpoint")
for url in [
    "https://www.popads.net/users/refer/1",
    "https://propellerads.com/publishers/",
    "https://www.exoclick.com/?login",
    "https://ad.doubleclick.net/ddm/clk/1;2;3",
    "https://www.googleadservices.com/pagead/aclk?sa=L",
]:
    add("ad", "link", url, note="link to an ad network")

# tracker
for url, kind in [
    ("https://www.googletagmanager.com/gtag/js?id=G-8RLHXWV1FL", "script"),
    ("https://www.google-analytics.com/analytics.js", "script"),
    ("https://www.google-analytics.com/g/collect?v=2&tid=G-1", "ping"),
    ("https://static.cloudflareinsights.com/beacon.min.js/v31e", "script"),
    ("https://connect.facebook.net/en_US/fbevents.js", "script"),
    ("https://www.facebook.com/tr?id=1&ev=PageView", "image"),
    ("https://s10.histats.com/js15_as.js", "script"),
    ("https://sstatic1.histats.com/0.gif?1&101", "image"),
    ("https://www.statcounter.com/counter/counter.js", "script"),
    ("https://c.statcounter.com/123/0/abc/1/", "image"),
    ("https://mc.yandex.ru/metrika/tag.js", "script"),
    ("https://mc.yandex.ru/watch/123", "image"),
    ("https://www.clarity.ms/tag/abc123", "script"),
    ("https://static.hotjar.com/c/hotjar-1.js?sv=6", "script"),
    ("https://script.hotjar.com/modules.js", "script"),
    ("https://cdn.segment.com/analytics.js/v1/abc/analytics.min.js", "script"),
    ("https://api.mixpanel.com/track/?data=abc", "xhr"),
    ("https://bat.bing.com/bat.js", "script"),
    ("https://sb.scorecardresearch.com/beacon.js", "script"),
    ("https://pixel.quantserve.com/pixel/p-abc.gif", "image"),
    ("https://analytics.tiktok.com/i18n/pixel/events.js", "script"),
    ("https://snap.licdn.com/li.lms-analytics/insight.min.js", "script"),
    ("https://stats.wp.com/e-202640.js", "script"),
    ("https://pixel.wp.com/g.gif?v=ext", "image"),
    ("https://cdn.matomo.cloud/site.matomo.cloud/matomo.js", "script"),
]:
    add("tracker", "request", url, kind, note="analytics / tracking endpoint")

# navigation (hand-picked listing/pagination links of content sites)
for url in [
    "https://www.moviesda.farm/tamil-2026-movies/page/2/",
    "https://www.moviesda.farm/category/tamil-dubbed-movies/",
    "https://www.tamilprint67.art/movies?genre=eng",
    "https://movflix.my/category/18/",
    "https://movflix.my/browse/",
    "https://www.isaimini.farm/tamil-movies/a-z/",
    "https://isaidub.love/movie/tamil-dubbed-movies-download/?page=3",
    "https://www.1tamilmv.army/index.php?/forums/forum/11-web-hd-itunes-hd-bluray/",
    "https://www.moviesda.info/?m=1",
    "https://moviesda17.com/search/leo/",
]:
    add("navigation", "link", url, page=None, note="listing / pagination / search page")

# redirects: requested → final (context redirect, first party = requested)
for requested, final, label in [
    ("https://www.moviesda.farm/", "https://www.moviesda.red/", "content"),
    ("https://isaimini.com.in/", "https://www.isaimini.farm/", "content"),
    ("https://kuttymovies1.fit/", "https://kuttymovies1.net/", "content"),
    ("https://tamilrockers.website/", "https://www.tamilrockers2024.com/", "content"),
    ("https://www.1tamilmv.army/", "https://www.1tamilmv.lc/", "content"),
    ("https://dl1.hotshare.click/f/1", "https://dl6.hotshare.click/f/1", "media"),
    ("https://linkurl.click/moviesda2026", "https://www.moviesda.farm/", "content"),
    ("https://www.moviesda.farm/go/1", "https://c1.popads.net/pop.php?z=1", "ad"),
    ("https://movflix.my/out", "https://www.popcash.net/go/1", "ad"),
    ("https://moviepie.xyz/go", "https://syndication.exoclick.com/splash.php?idzone=2", "ad"),
]:
    add(label, "redirect", final, page=requested, note="redirect final URL")

# social / reference: real content, out of scope by explicit policy (V1 F-1)
for url in [
    "https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fserpinsight.com",
    "https://twitter.com/share?url=http://www.tamilprint67.art/movies",
    "https://t.me/moviesdacloud",
    "https://www.linkedin.com/sharing/share-offsite/?url=x",
    "https://en.wikipedia.org/wiki/Leo_(2023_film)",
    "https://www.imdb.com/title/tt15654328/",
    "https://www.youtube.com/watch?v=qQlr9-rF32A",
    "https://www.reddit.com/r/movies/",
    "https://www.instagram.com/p/abc/",
    "https://www.pinterest.com/pin/create/button/?url=x",
]:
    add("content", "link", url, note="out_of_scope social/reference (explicit policy)")

# adversarial: content-source URLs that look like ad/tracker URLs
for url, kind, ctx, label in [
    ("https://www.moviesda.farm/ads-free-movie-2026/", "document", "link", "content"),
    ("https://www.moviesda.farm/banner-movie-2025-tamil/", "document", "link", "content"),
    ("https://www.tamilprint67.art/movie/track-list-songs", "document", "link", "content"),
    ("https://isaidub.love/movie/the-advert-2024-tamil-dubbed/", "document", "link", "content"),
    ("https://isaidub.love/movie/popup-2023-tamil/", "document", "link", "content"),
    ("https://www.moviesda.farm/sponsored-2026-tamil-movie/", "document", "link", "content"),
    ("https://movflix.my/movie/tracker-2024/", "document", "link", "content"),
    ("https://movflix.my/movie/the-ad-man/", "document", "link", "content"),
    ("https://movflix.my/movie/analytics-2025/", "document", "link", "content"),
    ("https://www.moviesda.farm/pixel-perfect-2026/", "document", "link", "content"),
    ("https://kuttymovies1.fit/click-2006-hindi-dubbed/", "document", "link", "content"),
    ("https://www.isaimini.farm/beacon-2025/", "document", "link", "content"),
    ("https://moviesda17.com/ad/leo-2023/", "document", "link", "content"),
    ("https://moviesda17.com/ads/leo-2023/", "document", "link", "content"),
    ("https://moviesda17.com/banner/leo-2023/", "document", "link", "content"),
    ("https://linkurl.click/moviesda2026", "document", "link", "navigation"),
    ("https://nowgoal.click/", "document", "link", "unknown"),
    ("https://data527.click/", "document", "link", "unknown"),
    ("https://adserver.hotshare.click/file/x.mp4", "document", "link", "media"),
    ("https://popup.movcloud.click/v/x/master.m3u8", "document", "link", "media"),
    (
        "https://www.moviesda.farm/wp-content/uploads/2026/ads/poster.jpg",
        "image",
        "request",
        "content",
    ),
    (
        "https://www.moviesda.farm/wp-content/uploads/banner/leo-poster.jpg",
        "image",
        "request",
        "content",
    ),
    ("https://myimg.click/images/2026/07/20/Advert-2026.jpg", "image", "request", "content"),
    ("https://imgshare.info/images/2026/02/26/Popup-2025.jpg", "image", "request", "content"),
    ("https://www.moviesda.farm/wp-content/themes/x/js/track.js", "script", "request", "unknown"),
    ("https://new1.movcloud.click/v/tracking/master.m3u8", "media", "request", "media"),
    ("https://streamtape.com/get_video?id=abc&expires=1&ip=x&token=y", "media", "request", "media"),
    ("https://dood.watch/pass_md5/abc/def", "xhr", "request", "player"),
    ("https://filemoon.sx/dl?op=view&file_code=abc&hash=x", "xhr", "request", "player"),
    ("https://cdn.jwplayer.com/v2/media/AbC123", "xhr", "request", "player"),
]:
    HAND.append(
        {
            "label": label,
            "context": ctx,
            "url": url,
            "resource_type": kind,
            "first_party": P if ctx == "request" or label != "navigation" else None,
            "synthetic": True,
            "note": "adversarial: ad/tracker-like words on a content source",
        }
    )


def sampled() -> list[dict[str, object]]:
    rng = random.Random(606)  # noqa: S311 -- reproducible sample, not security
    links: dict[str, dict[str, object]] = {}
    requests: dict[str, dict[str, object]] = {}
    for page in corpus.load(CAPTURE):
        base = page.final_url
        tree = LexborHTMLParser(page.body)
        page_host = host_of(base) or ""
        for node in tree.css("a[href]"):
            url = urljoin(base, node.attributes.get("href") or "").partition("#")[0]
            host = host_of(url)
            if not host or registrable_domain(host) != registrable_domain(page_host):
                continue
            path = urlsplit(url).path + ("?" + urlsplit(url).query if urlsplit(url).query else "")
            label = (
                "navigation"
                if any(m in path.lower() for m in _NAV_MARKERS) or path in ("", "/")
                else "content"
            )
            links.setdefault(
                url,
                {
                    "label": label,
                    "context": "link",
                    "url": url,
                    "resource_type": "document",
                    "first_party": base,
                    "synthetic": False,
                    "note": "same-site link on a W691 page",
                },
            )
        for selector, attr, kind in (
            ("script[src]", "src", "script"),
            ("img[src]", "src", "image"),
            ("iframe[src]", "src", "document"),
            ("link[rel=stylesheet][href]", "href", "stylesheet"),
        ):
            for node in tree.css(selector):
                url = urljoin(base, node.attributes.get(attr) or "")
                host = host_of(url)
                if not host:
                    continue
                domain = registrable_domain(host)
                label = REQUEST_LABELS.get(domain) or REQUEST_LABELS.get(host.split(".", 1)[-1])
                if label is None or domain == registrable_domain(page_host):
                    continue
                requests.setdefault(
                    domain + "|" + kind,
                    {
                        "label": label,
                        "context": "request",
                        "url": url,
                        "resource_type": kind,
                        "first_party": base,
                        "synthetic": False,
                        "note": f"observed third-party request ({domain})",
                    },
                )
    content = sorted(links.values(), key=lambda x: str(x["url"]))
    rng.shuffle(content)
    return content[:SAMPLED_CONTENT] + sorted(requests.values(), key=lambda x: str(x["url"]))


def main() -> None:
    items = sampled() + HAND
    seen, unique = set(), []
    for item in items:
        key = (item["context"], item["url"], item["first_party"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    doc = {
        "description": (
            "P6 labelled corpus (design §21, Gate E). Labels assigned by hand before evaluation."
        ),
        "labels": sorted({str(i["label"]) for i in unique}),
        "items": unique,
    }
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    from collections import Counter

    print(len(unique), Counter((i["label"], i["synthetic"]) for i in unique))


if __name__ == "__main__":
    main()
