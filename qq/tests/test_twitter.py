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


def test_images_are_photos_then_video_stills_at_chat_size():
    link = twitter.find("https://x.com/2SC1815J/status/2116527795329495151")[0]
    post = twitter.parse(TWEET, link)
    assert twitter.image_urls(post) == [
        "https://pbs.twimg.com/media/HV9qRG9aoAAsgDx.png?name=large",
        "https://pbs.twimg.com/ext_tw_video_thumb/1.jpg?name=large",
    ]


def test_images_stop_at_the_limit():
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
    assert len(twitter.image_urls(post, limit=2)) == 2


def test_sensitive_post_is_marked_and_shows_no_pictures():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {**TWEET, "id": "1", "possibly_sensitive": True}
    post = twitter.parse(tweet, link)
    text = twitter.format_post(post)
    assert "[sensitive]" in text
    assert twitter.image_urls(post)


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
    assert twitter.image_urls(post) == []


def test_images_outside_the_twitter_picture_host_are_ignored():
    link = twitter.find("https://x.com/a/status/1")[0]
    tweet = {
        "id": "1",
        "author": {"screen_name": "a", "name": "a"},
        "media": {
            "all": [
                {"type": "photo", "url": "https://internal.example/photo.jpg"},
                {"type": "photo", "url": "https://pbs.twimg.com/media/ok.jpg"},
            ]
        },
    }
    post = twitter.parse(tweet, link)
    assert twitter.image_urls(post) == [
        "https://pbs.twimg.com/media/ok.jpg?name=large"
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
    assert twitter.image_urls(post) == [
        "https://pbs.twimg.com/media/a.jpg?name=large"
    ]


def test_markdown_punctuation_around_a_link_is_kept():
    assert twitter.find("`https://x.com/a/status/1`")[0].status == "1"
    assert twitter.rewrite("`https://x.com/a/status/1`") == (
        "`https://fixupx.com/a/status/1`"
    )
