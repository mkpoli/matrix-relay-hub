import asyncio
import datetime

from qq_bridge import twitter

TWEET = {
    "id": "2116527795329495151",
    "url": "https://x.com/2SC1815J/status/2116527795329495151",
    "text": "そあん v2.0 の処理について。",
    "created_at": "Sat Oct 31 13:48:44 +0000 2026",
    "author": {"screen_name": "2SC1815J", "name": "2SC1815J"},
    "media": {
        "all": [
            {
                "type": "photo",
                "url": "https://pbs.twimg.com/media/HV9qRG9aoAAsgDx.png?name=orig",
            },
            {
                "type": "video",
                "url": "https://video.twimg.com/x.mp4",
                "thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/1.jpg",
            },
        ]
    },
    "quote": {
        "text": "引用された投稿",
        "author": {"screen_name": "quoted", "name": "Quoted Name"},
    },
    "poll": {
        "choices": [
            {"label": "はい", "percentage": 60},
            {"label": "いいえ", "percentage": 40},
        ]
    },
    "possibly_sensitive": False,
    "replying_to": None,
}


def test_find_accepts_the_link_forms_people_post():
    text = (
        "https://x.com/Alice/status/123 "
        "https://twitter.com/Bob/status/456? s=20 "
        "https://mobile.x.com/Carol/status/789/photo/1 "
        "https://x.com/i/web/status/1011"
    )
    links = twitter.find(text)
    assert [(link.handle, link.status) for link in links] == [
        ("Alice", "123"),
        ("Bob", "456"),
        ("Carol", "789"),
        ("i", "1011"),
    ]


def test_find_leaves_other_links_and_words_alone():
    text = "https://fixupx.com/Alice/status/123 https://notx.com/Alice/status/123 x.com/Alice/status/123"
    assert twitter.find(text) == []


def test_find_returns_a_repeated_status_once():
    assert len(twitter.find("https://x.com/a/status/1 https://twitter.com/a/status/1")) == 1


def test_an_x_link_inside_another_urls_query_is_left_alone():
    text = "https://example.org/?next=https://x.com/Alice/status/123"
    assert twitter.find(text) == []
    assert twitter.rewrite(text) == text


def test_rewrite_replaces_every_repeated_link():
    text = "https://x.com/a/status/1 and https://twitter.com/a/status/1"
    assert twitter.rewrite(text) == (
        "https://fixupx.com/a/status/1 and https://fixupx.com/a/status/1"
    )


def test_rewrite_keeps_the_surrounding_message():
    text = "look: https://x.com/Alice/status/123/photo/1, then https://example.org/"
    assert twitter.rewrite(text) == (
        "look: https://fixupx.com/Alice/status/123, then https://example.org/"
    )


def test_api_and_fixup_urls_use_the_same_handle_and_status():
    link = twitter.find("https://mobile.twitter.com/Alice/status/123")[0]
    assert link.api_url == "https://api.fxtwitter.com/Alice/status/123"
    assert link.fixup_url == "https://fixupx.com/Alice/status/123"


def test_parse_reads_the_post_and_format_shows_its_parts():
    link = twitter.find("https://x.com/2SC1815J/status/2116527795329495151")[0]
    post = twitter.parse(TWEET, link)
    assert post is not None
    assert post.created == datetime.datetime(
        2026, 10, 31, 13, 48, 44, tzinfo=datetime.timezone.utc
    )
    text = twitter.format_post(post)
    assert text.startswith("[Twitter] 2SC1815J (@2SC1815J) · 2026-10-31 21:48")
    assert "そあん v2.0 の処理について。" in text
    assert "Quoting Quoted Name (@quoted): 引用された投稿" in text
    assert "Poll: はい (60%) · いいえ (40%)" in text
    assert text.endswith("https://fixupx.com/2SC1815J/status/2116527795329495151")


def test_parse_rejects_a_post_that_is_not_the_link():
    link = twitter.find("https://x.com/Alice/status/999")[0]
    assert twitter.parse(TWEET, link) is None


def test_media_is_photos_then_playable_videos():
    link = twitter.find("https://x.com/2SC1815J/status/2116527795329495151")[0]
    post = twitter.parse(TWEET, link)
    assert twitter.attachments(post) == [
        twitter.Attachment(
            "image", "https://pbs.twimg.com/media/HV9qRG9aoAAsgDx.png?name=large"
        ),
        twitter.Attachment("video", "https://video.twimg.com/x.mp4"),
    ]


def test_media_stops_at_the_limit():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {"type": "photo", "url": f"https://pbs.twimg.com/media/{n}.jpg"}
                for n in range(6)
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert len(twitter.attachments(post, limit=2)) == 2


def test_sensitive_post_is_marked_and_shows_no_media():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {**TWEET, "id": "1", "possibly_sensitive": True}
    post = twitter.parse(tweet, link)
    text = twitter.format_post(post)
    assert "[sensitive]" in text
    assert twitter.attachments(post)


def test_plain_http_pictures_are_ignored():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [{"type": "photo", "url": "http://pbs.twimg.com/media/a.jpg"}]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.attachments(post) == []


def test_media_outside_the_twitter_hosts_are_ignored():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {"type": "photo", "url": "https://internal.example/photo.jpg"},
                {"type": "photo", "url": "https://pbs.twimg.com/media/ok.jpg"},
                {"type": "video", "url": "https://internal.example/video.mp4"},
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.attachments(post) == [
        twitter.Attachment("image", "https://pbs.twimg.com/media/ok.jpg?name=large")
    ]


def test_malformed_media_does_not_drop_the_post_text():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "text": "still here",
        "author": {"screen_name": "a", "name": "a"},
        "media": {"all": 3},
    }
    post = twitter.parse(tweet, link)
    assert post.text == "still here"
    assert post.media == ()


def test_photos_are_used_when_all_is_absent():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "photos": [
                {"url": "https://pbs.twimg.com/media/a.jpg?name=orig"},
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.attachments(post) == [
        twitter.Attachment("image", "https://pbs.twimg.com/media/a.jpg?name=large")
    ]


def _video_tweet(duration, formats, **media):
    return {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {
                    "type": "video",
                    "url": "https://video.twimg.com/high.mp4",
                    "thumbnail_url": "https://pbs.twimg.com/thumb/1.jpg",
                    "duration": duration,
                    "formats": formats,
                    **media,
                }
            ]
        },
    }


def test_gif_forwards_its_mp4_instead_of_the_still():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {
                    "type": "gif",
                    "url": "https://pbs.twimg.com/tweet_video_thumb/a.jpg",
                    "duration": 3.2,
                    "formats": [
                        {
                            "url": "https://video.twimg.com/tweet_video/a.mp4",
                            "bitrate": 832000,
                            "container": "mp4",
                            "codec": "h264",
                        }
                    ],
                }
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.attachments(post) == [
        twitter.Attachment("video", "https://video.twimg.com/tweet_video/a.mp4")
    ]


def test_video_variants_are_ranked_h264_first_and_best_first():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = _video_tweet(
        10,
        [
            {"url": "https://video.twimg.com/480.mp4", "bitrate": 256000},
            {"url": "https://video.twimg.com/720.mp4", "bitrate": 832000},
            {"url": "https://video.twimg.com/1080.mp4", "bitrate": 5000000},
        ],
    )
    post = twitter.parse(tweet, link)
    assert post.media[0].variants == (
        "https://video.twimg.com/1080.mp4",
        "https://video.twimg.com/720.mp4",
        "https://video.twimg.com/480.mp4",
    )
    assert twitter.attachments(post)[0].url == "https://video.twimg.com/1080.mp4"
    assert twitter.attachments(post)[0].candidates == (
        "https://video.twimg.com/1080.mp4",
        "https://video.twimg.com/720.mp4",
        "https://video.twimg.com/480.mp4",
    )


def _sizes(monkeypatch, table):
    async def size(session, url):
        return table.get(url)

    monkeypatch.setattr("qq_bridge.twitter._content_length", size)


def test_the_best_variant_that_fits_qq_is_tried_first(monkeypatch):
    _sizes(
        monkeypatch,
        {
            "https://video.twimg.com/4k.mp4": 8_000_000,
            "https://video.twimg.com/hd.mp4": 4_000_000,
        },
    )
    assert asyncio.run(
        twitter._ranked(
            object(),
            ("https://video.twimg.com/4k.mp4", "https://video.twimg.com/hd.mp4"),
        )
    ) == ["https://video.twimg.com/4k.mp4", "https://video.twimg.com/hd.mp4"]


def test_the_smallest_variant_is_tried_first_when_none_fit(monkeypatch):
    _sizes(
        monkeypatch,
        {
            "https://video.twimg.com/4k.mp4": 40_000_000,
            "https://video.twimg.com/hd.mp4": 20_000_000,
        },
    )
    assert asyncio.run(
        twitter._ranked(
            object(),
            ("https://video.twimg.com/4k.mp4", "https://video.twimg.com/hd.mp4"),
        )
    ) == ["https://video.twimg.com/hd.mp4", "https://video.twimg.com/4k.mp4"]


def test_the_best_variant_is_tried_first_when_sizes_are_unknown(monkeypatch):
    _sizes(monkeypatch, {})
    assert asyncio.run(
        twitter._ranked(
            object(),
            ("https://video.twimg.com/4k.mp4", "https://video.twimg.com/hd.mp4"),
        )
    ) == ["https://video.twimg.com/4k.mp4", "https://video.twimg.com/hd.mp4"]


def test_download_falls_back_to_the_next_variant(monkeypatch):
    big = "https://video.twimg.com/4k.mp4"
    small = "https://video.twimg.com/hd.mp4"

    async def ranked(session, candidates, limit=0):
        return [big, small]

    async def fetch(session, url, kind, hosts, prefix, limit):
        return (b"mp4", "video/mp4") if url == small else None

    monkeypatch.setattr("qq_bridge.twitter._ranked", ranked)
    monkeypatch.setattr("qq_bridge.twitter._fetch", fetch)
    got = asyncio.run(
        twitter.download(object(), twitter.Attachment("video", big, (big, small)))
    )
    assert got == (b"mp4", "video/mp4")


def test_hevc_av1_vp9_and_playlists_are_skipped_for_a_takeable_mp4():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = _video_tweet(
        10,
        [
            {"url": "https://video.twimg.com/playlist.m3u8", "container": "m3u8"},
            {
                "url": "https://video.twimg.com/hevc/1080.mp4",
                "bitrate": 9000000,
                "codec": "hevc",
            },
            {
                "url": "https://video.twimg.com/av1/1080.mp4",
                "bitrate": 8000000,
                "codec": "av1",
            },
            {
                "url": "https://video.twimg.com/vp9/1080.mp4",
                "bitrate": 7000000,
                "codec": "vp9",
            },
            {
                "url": "https://video.twimg.com/avc1/480.mp4",
                "bitrate": 256000,
                "codec": "h264",
            },
        ],
    )
    post = twitter.parse(tweet, link)
    assert post.media[0].variants == ("https://video.twimg.com/avc1/480.mp4",)
    assert twitter.attachments(post) == [
        twitter.Attachment("video", "https://video.twimg.com/avc1/480.mp4")
    ]


def test_a_video_without_a_playable_mp4_falls_back_to_its_still():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {
                    "type": "video",
                    "url": "https://video.twimg.com/a.m3u8",
                    "thumbnail_url": "https://pbs.twimg.com/thumb/a.jpg",
                    "formats": [
                        {"url": "https://video.twimg.com/a.m3u8", "container": "m3u8"}
                    ],
                }
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.attachments(post) == [
        twitter.Attachment("image", "https://pbs.twimg.com/thumb/a.jpg?name=large")
    ]


def test_attachments_drop_a_video_off_the_video_host():
    link = twitter.find("https://x.com/a/status/1")[0]
    post = twitter.Post(
        handle="a",
        name="a",
        text="",
        created=None,
        link=link,
        media=(
            twitter.Media(
                "video",
                "https://internal.example/x.mp4",
                "https://pbs.twimg.com/thumb/a.jpg",
            ),
        ),
        quote=None,
        poll=(),
        sensitive=False,
        replying_to=None,
    )
    assert twitter.attachments(post) == [
        twitter.Attachment("image", "https://pbs.twimg.com/thumb/a.jpg?name=large")
    ]


def test_markdown_punctuation_around_a_link_is_kept():
    assert twitter.find("`https://x.com/a/status/1`")[0].status == "1"
    assert twitter.rewrite("`https://x.com/a/status/1`") == (
        "`https://fixupx.com/a/status/1`"
    )
