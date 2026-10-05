python
import json
import os
import re
import sys
import time
from pathlib import Path

import requests


# ============================================================
# Configuration
# ============================================================

BLUESKY_HANDLE = "bloxy.news"
BLUESKY_API = "https://public.api.bsky.app"

MASTODON_BASE = "https://mastodon.social"

STATE_FILE = Path("state.json")

REQUEST_TIMEOUT = 30
BLUESKY_PAGE_SIZE = 100
MASTODON_PAGE_SIZE = 40

# Maximum number of pages we will walk looking for old data.
MAX_PAGES = 100

REQUEST_DELAY = 1.0

USER_AGENT = "BloxyNews-Mastodon-Mirror/1.0"


# ============================================================
# HTTP
# ============================================================

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
})


def mastodon_headers():
    token = os.environ.get("MASTODON_TOKEN")

    if not token:
        print("ERROR: MASTODON_TOKEN is not set.")
        sys.exit(1)

    return {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }


def get_json(url, **kwargs):
    response = session.get(
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )

    if not response.ok:
        print(f"GET {url} failed: {response.status_code}")
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()


# ============================================================
# State
# ============================================================

def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False,
        )
        file.write("\n")

    temporary.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return None

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            return json.load(file)

    except (OSError, json.JSONDecodeError) as exc:
        print(f"WARNING: Could not load state.json: {exc}")
        return None


def new_state():
    return {
        "version": 1,
        "last_seen_uri": None,
        "mirrored": {},
    }


# ============================================================
# Bluesky
# ============================================================

def get_author_feed(cursor=None):
    params = {
        "actor": BLUESKY_HANDLE,
        "limit": BLUESKY_PAGE_SIZE,
    }

    if cursor:
        params["cursor"] = cursor

    return get_json(
        f"{BLUESKY_API}/xrpc/app.bsky.feed.getAuthorFeed",
        params=params,
    )


def is_repost(feed_item):
    # getAuthorFeed includes reposts.
    # A repost has a "reason" field.
    return feed_item.get("reason") is not None


def extract_posts_from_feed(data):
    posts = []

    for item in data.get("feed", []):
        if is_repost(item):
            continue

        post = item.get("post")

        if post:
            posts.append(post)

    return posts


def get_newest_bluesky_post():
    data = get_author_feed()

    posts = extract_posts_from_feed(data)

    if not posts:
        return None

    return posts[0]


def get_posts_since(last_seen_uri):
    """
    Fetch posts newest -> oldest until last_seen_uri is found.

    Returned posts are sorted oldest -> newest so replies can
    be posted after their parents.

    If the previous mirrored post has been deleted from Bluesky,
    only the current newest post is returned for recovery.
    """

    found = []
    cursor = None

    for page_number in range(MAX_PAGES):
        data = get_author_feed(cursor)

        posts = extract_posts_from_feed(data)

        if not posts:
            break

        for post in posts:
            uri = post.get("uri")

            if not uri:
                continue

            if uri == last_seen_uri:
                found.sort(
                    key=lambda p: p["record"]["createdAt"]
                )
                return found

            found.append(post)

        cursor = data.get("cursor")

        if not cursor:
            break

        print(
            f"Fetched Bluesky page {page_number + 1}"
        )

    # The previous mirrored post no longer exists in the
    # Bluesky feed. This most likely means it was deleted.
    #
    # Do not replay all of the posts we just fetched because
    # that could duplicate a large amount of the mirror.
    print()
    print(
        "WARNING: Could not find the previous mirrored Bluesky "
        "post in the author feed."
    )
    print(
        "Assuming the previous post was deleted."
    )

    newest = get_newest_bluesky_post()

    if newest is None:
        print(
            "ERROR: No Bluesky posts found."
        )
        sys.exit(1)

    print(
        f"Using newest Bluesky post for recovery: "
        f"{newest['uri']}"
    )

    return [newest]


# ============================================================
# Bluesky URLs / post IDs
# ============================================================

def bluesky_uri_to_url(uri):
    """
    Convert an AT URI into a normal Bluesky post URL.

    Example:

    at://did:plc:xxxxx/app.bsky.feed.post/abc

    ->

    https://bsky.app/profile/bloxy.news/post/abc
    """

    rkey = uri.rsplit("/", 1)[-1]

    return (
        f"https://bsky.app/profile/"
        f"{BLUESKY_HANDLE}/post/{rkey}"
    )


def bluesky_rkey(uri):
    return uri.rsplit("/", 1)[-1]


# ============================================================
# Bluesky replies
# ============================================================

def get_parent_uri(post):
    record = post.get("record", {})

    reply = record.get("reply")

    if not reply:
        return None

    parent = reply.get("parent")

    if not parent:
        return None

    return parent.get("uri")


# ============================================================
# Bluesky images
# ============================================================

def get_images(post):
    embed = post.get("embed")

    if not embed:
        return []

    embed_type = embed.get("$type", "")

    if embed_type.endswith("images#view"):
        return embed.get("images", [])

    if embed_type.endswith("recordWithMedia#view"):
        media = embed.get("media", {})

        if media.get("$type", "").endswith(
            "images#view"
        ):
            return media.get("images", [])

    return []


# ============================================================
# Mastodon account
# ============================================================

def get_mastodon_account():
    response = session.get(
        f"{MASTODON_BASE}/api/v1/accounts/verify_credentials",
        headers=mastodon_headers(),
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        print("Mastodon authentication failed.")
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()


# ============================================================
# Mastodon statuses
# ============================================================

def get_mastodon_statuses(
    account_id,
    max_id=None,
):
    params = {
        "limit": MASTODON_PAGE_SIZE,
        "exclude_reblogs": "true",
    }

    if max_id:
        params["max_id"] = max_id

    response = session.get(
        f"{MASTODON_BASE}/api/v1/accounts/"
        f"{account_id}/statuses",
        headers=mastodon_headers(),
        params=params,
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        print("Failed to read Mastodon statuses.")
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()


def get_all_mastodon_statuses(account_id):
    """
    Read the mirror account's statuses.

    This is mainly used for recovery when the Actions cache
    is missing.
    """

    statuses = []
    max_id = None

    for page_number in range(MAX_PAGES):
        batch = get_mastodon_statuses(
            account_id,
            max_id=max_id,
        )

        if not batch:
            break

        statuses.extend(batch)

        max_id = batch[-1]["id"]

        print(
            f"Fetched Mastodon page {page_number + 1} "
            f"({len(statuses)} statuses)"
        )

        if len(batch) < MASTODON_PAGE_SIZE:
            break

    return statuses


# ============================================================
# Recovery from Mastodon history
# ============================================================

SOURCE_URL_RE = re.compile(
    r"https://bsky\.app/profile/"
    r"bloxy\.news/post/"
    r"([A-Za-z0-9._-]+)"
)


def extract_source_rkey(status):
    """
    Find the Bluesky post key from our Source: URL.
    """

    content = status.get("content", "")

    match = SOURCE_URL_RE.search(content)

    if not match:
        return None

    return match.group(1)


def rebuild_state_from_mastodon(statuses):
    """
    Reconstruct our state entirely from existing Mastodon
    posts.

    Returns None if the account contains no mirror posts.
    """

    mirrored = {}

    # Mastodon returns newest first.
    newest_source_uri = None

    for status in statuses:
        rkey = extract_source_rkey(status)

        if not rkey:
            continue

        uri = f"rkey:{rkey}"

        mirrored[uri] = status["id"]

        if newest_source_uri is None:
            newest_source_uri = uri

    if not mirrored:
        return None

    return {
        "version": 1,
        "last_seen_uri": newest_source_uri,
        "mirrored": mirrored,
    }


# ============================================================
# Mastodon instance limits
# ============================================================

def get_mastodon_limits():
    data = get_json(
        f"{MASTODON_BASE}/api/v2/instance"
    )

    configuration = data.get(
        "configuration",
        {},
    )

    status_config = configuration.get(
        "statuses",
        {},
    )

    media_config = configuration.get(
        "media_attachments",
        {},
    )

    return {
        "max_characters": status_config.get(
            "max_characters",
            500,
        ),
        "max_media": status_config.get(
            "max_media_attachments",
            4,
        ),
        "image_size_limit": media_config.get(
            "image_size_limit",
            16 * 1024 * 1024,
        ),
    }


# ============================================================
# Mastodon media
# ============================================================

def upload_image(image, max_size):
    image_url = image.get("fullsize")

    if not image_url:
        return None

    alt_text = image.get("alt", "")

    print(
        "    Downloading image..."
    )

    response = session.get(
        image_url,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    content = response.content

    if len(content) > max_size:
        print(
            "    Image is larger than Mastodon's limit; "
            "skipping it."
        )
        return None

    content_type = response.headers.get(
        "Content-Type",
        "image/jpeg",
    )

    files = {
        "file": (
            "bloxynews-image",
            content,
            content_type,
        )
    }

    data = {
        "description": alt_text[:1500],
    }

    response = session.post(
        f"{MASTODON_BASE}/api/v2/media",
        headers=mastodon_headers(),
        files=files,
        data=data,
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        print(
            "    Media upload failed:"
        )
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()["id"]


# ============================================================
# Mastodon status creation
# ============================================================

def build_status_text(
    post,
    max_characters,
):
    record = post.get("record", {})

    text = record.get(
        "text",
        "",
    ).strip()

    source = (
        "\n\nSource: "
        + bluesky_uri_to_url(post["uri"])
    )

    if len(text) + len(source) <= max_characters:
        return text + source

    allowed_text = max_characters - len(source)

    if allowed_text <= 1:
        return source[:max_characters]

    text = text[:allowed_text - 1] + "…"

    return text + source


def create_mastodon_status(
    post,
    limits,
    parent_mastodon_id=None,
):
    text = build_status_text(
        post,
        limits["max_characters"],
    )

    media_ids = []

    for image in get_images(post)[
        :limits["max_media"]
    ]:
        try:
            media_id = upload_image(
                image,
                limits["image_size_limit"],
            )

            if media_id:
                media_ids.append(media_id)

        except requests.RequestException as exc:
            print(
                f"    Failed to upload image: {exc}"
            )

    data = {
        "status": text,
        "visibility": "public",
    }

    if parent_mastodon_id:
        data["in_reply_to_id"] = parent_mastodon_id

    if media_ids:
        data["media_ids[]"] = media_ids

    # The Bluesky URI is unique, so it makes a useful
    # idempotency key.
    idempotency_key = (
        "bloxynews-" +
        str(abs(hash(post["uri"])))
    )

    response = session.post(
        f"{MASTODON_BASE}/api/v1/statuses",
        headers={
            **mastodon_headers(),
            "Idempotency-Key": idempotency_key,
        },
        data=data,
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        print(
            "Mastodon status creation failed:"
        )
        print(response.text[:1000])
        response.raise_for_status()

    return response.json()


# ============================================================
# Main
# ============================================================

def main():
    print()
    print("====================================")
    print(" Roblox RTC -> Mastodon Mirror")
    print("====================================")
    print()

    # --------------------------------------------------------
    # Authenticate with Mastodon.
    # --------------------------------------------------------

    account = get_mastodon_account()

    account_name = account.get(
        "acct",
        account.get("username", "unknown"),
    )

    account_id = account["id"]

    print(
        f"Mastodon account: @{account_name}"
    )

    # --------------------------------------------------------
    # Load cached state.
    # --------------------------------------------------------

    state = load_state()

    # --------------------------------------------------------
    # Recovery if cache disappeared.
    # --------------------------------------------------------

    if state is None:
        print(
            "No cached state found."
        )

        existing_statuses = (
            get_all_mastodon_statuses(account_id)
        )

        rebuilt = rebuild_state_from_mastodon(
            existing_statuses
        )

        if rebuilt is not None:
            state = rebuilt

            print(
                "Recovered mirror state from Mastodon."
            )

            save_state(state)

        else:
            # ------------------------------------------------
            # REAL FIRST RUN
            # ------------------------------------------------

            newest = get_newest_bluesky_post()

            if newest is None:
                print(
                    "No Bluesky posts found."
                )
                return

            state = new_state()

            state["last_seen_uri"] = (
                newest["uri"]
            )

            save_state(state)

            print()
            print(
                "FIRST RUN INITIALIZED."
            )
            print(
                "The current newest Bluesky post was recorded."
            )
            print(
                "No historical posts were mirrored."
            )
            print()
            return

    # --------------------------------------------------------
    # Make sure the state is usable.
    # --------------------------------------------------------

    last_seen_uri = state.get(
        "last_seen_uri"
    )

    if not last_seen_uri:
        print(
            "ERROR: State exists but has no last_seen_uri."
        )
        sys.exit(1)

    mirrored = state.setdefault(
        "mirrored",
        {},
    )

    # --------------------------------------------------------
    # Find new Bluesky posts.
    # --------------------------------------------------------

    print(
        f"Checking since: {last_seen_uri}"
    )

    posts = get_posts_since(
        last_seen_uri
    )

    if not posts:
        print(
            "No new Bluesky posts."
        )
        return

    print(
        f"Found {len(posts)} new Bluesky posts."
    )
    print()

    # --------------------------------------------------------
    # Mastodon limits.
    # --------------------------------------------------------

    limits = get_mastodon_limits()

    # --------------------------------------------------------
    # Mirror posts oldest -> newest.
    # --------------------------------------------------------

    for post in posts:
        uri = post["uri"]

        if uri in mirrored:
            print(
                f"Already mirrored {uri}; skipping."
            )

            state["last_seen_uri"] = uri
            save_state(state)

            continue

        parent_uri = get_parent_uri(post)

        parent_mastodon_id = None

        # ----------------------------------------------------
        # Replies to RTC's own posts
        # ----------------------------------------------------

        if parent_uri:
            parent_mastodon_id = mirrored.get(
                parent_uri
            )

            # If parent was mirrored before this run but
            # was not in the cached map, it may still be
            # recoverable by rkey.
            if parent_mastodon_id is None:
                parent_rkey = bluesky_rkey(
                    parent_uri
                )

                parent_mastodon_id = (
                    mirrored.get(
                        f"rkey:{parent_rkey}"
                    )
                )

            if parent_mastodon_id is None:
                print()
                print(
                    f"WARNING: {uri} is a reply, but its "
                    "parent is not in our mirror state."
                )
                print(
                    "Posting it as a standalone post instead."
                )

        # ----------------------------------------------------
        # Create Mastodon status.
        # ----------------------------------------------------

        print(
            f"Mirroring {uri}"
        )

        status = create_mastodon_status(
            post,
            limits,
            parent_mastodon_id,
        )

        mastodon_id = status["id"]

        print(
            f"  -> Mastodon status {mastodon_id}"
        )

        # Store both forms so recovery is easy.
        mirrored[uri] = mastodon_id
        mirrored[
            f"rkey:{bluesky_rkey(uri)}"
        ] = mastodon_id

        state["last_seen_uri"] = uri

        # Save after EVERY successful post.
        # If the workflow crashes halfway through, the
        # cache can still preserve all completed work.
        save_state(state)

        print()

        time.sleep(REQUEST_DELAY)

    print(
        "Mirror run complete."
    )


if __name__ == "__main__":
    main()
