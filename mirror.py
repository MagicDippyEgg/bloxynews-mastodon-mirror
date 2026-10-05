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
        print("ERROR: No Bluesky posts found.")
        sys.exit(1)

    # Return only the newest currently-existing post.
    # This prevents replaying the entire history.
    print(
        f"Using newest Bluesky post as recovery baseline: "
        f"{newest['uri']}"
    )

    return [newest]
