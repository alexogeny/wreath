"""Where image bytes live, and who is allowed to put them there.

A camera-trap network moves far more bytes than rows. A single deployment is a
few gigabytes of JPEGs; the database row that describes one of them is a few
hundred bytes. Routing the images through the application would make the
request path the narrowest part of the system for the one payload that is
guaranteed to be large, so the images never touch it: the application signs a
URL, the field team's uploader writes to that URL, and the application learns
about the bytes afterwards.

**The key layout is a decision, not a convention.** Keys are

    cards/{reserve_slug}/{deployment_id}/{card_serial}.zip     the archive
    images/{reserve_slug}/{deployment_id}/{entry_name}         one image

so the store can be listed by reserve, by deployment, or in full, and a
`delete` of one deployment's images is a prefix. That matters because the
retention rule in this domain is per-reserve — a permit expires, and everything
collected under it has to go — and a layout that cannot express the rule turns
a `DELETE` into a full scan.

`normalize_key` is what the store applies to every key it is handed, and it
refuses `..` segments, so a hostile `card_serial` cannot escape its prefix. The
functions here still build keys from a slug and an integer rather than from
caller text wherever they can, because a refusal at the boundary is better than
a refusal deep inside a write.

**On thumbnails.** `Sighting.thumbnail_key` exists and this stage leaves it
null, which is the honest outcome rather than an omission. Deriving a thumbnail
means decoding a JPEG, and wreath ships no image codec — a framework with no
mandatory runtime dependencies is not going to grow one. A real deployment
generates thumbnails in the ingest job with Pillow or an out-of-process
`vips`/`ffmpeg` call, writes them to `thumbnail_key`, and changes nothing else:
the column, the key layout and the job are already the right shape. The example
stops at the point where it would have to lie about what wreath does.
"""

from __future__ import annotations

from wreath.objects import ObjectStore, normalize_key

#: How long a minted upload URL stays valid.
#:
#: Fifteen minutes is long enough for a field laptop on a satellite link to
#: finish one card and short enough that a URL found in a shell history or a
#: proxy log is worthless. It is deliberately *not* configurable: the number
#: only makes sense against how long an upload takes, which the application
#: knows and an operator does not.
UPLOAD_URL_TTL = 900

#: The one content type an upload URL is minted for. Signed URLs authorise a
#: method and a key, not a media type, so this is what the *route* enforces --
#: see `routers.uploads`.
ARCHIVE_CONTENT_TYPE = "application/zip"


def card_key(reserve_slug: str, deployment_id: int, card_serial: str) -> str:
    """The key one deployment's uploaded card archive occupies.

    Args:
        reserve_slug: the owning reserve, so the store lists per permit.
        deployment_id: the collection event this card belongs to.
        card_serial: the SD card's serial, which makes two cards collected on
            the same day at the same station distinguishable.

    Returns:
        A normalised key. One deployment has one card, so re-uploading
        overwrites rather than accumulating -- a partial upload retried is the
        common case, and a store full of half-written archives is not worth the
        alternative.

    Raises:
        ObjectError: when the assembled key is not a valid key, which is what a
            `card_serial` containing a path separator produces.
    """
    return normalize_key(f"cards/{reserve_slug}/{deployment_id}/{card_serial}.zip")


def image_prefix(reserve_slug: str, deployment_id: int) -> str:
    """The prefix the archive's entries are unpacked under.

    Ends with `/` because `unzip_stream` prepends it verbatim rather than
    joining paths -- the separator is the caller's to supply, and forgetting it
    silently produces `images/kopje12` instead of `images/kopje/12`.
    """
    return f"images/{reserve_slug}/{deployment_id}/"


def mint_upload_url(store: ObjectStore, key: str) -> str:
    """A URL granting exactly one `PUT` of `key`, expiring in `UPLOAD_URL_TTL`.

    The method is part of the signature, so this URL cannot be replayed as a
    `GET` to read somebody else's card back out.

    Args:
        store: the store the URL is signed against.
        key: the object the URL grants a write of.

    Returns:
        An absolute URL carrying the key, the deadline and the signature.
    """
    return store.url(key, expires=UPLOAD_URL_TTL, method="PUT")
