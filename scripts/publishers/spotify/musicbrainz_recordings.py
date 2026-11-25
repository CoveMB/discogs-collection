"""Verify recording codes against a MusicBrainz pressing linked to Discogs."""

from __future__ import annotations

import http.client
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from publishers.spotify.matching import artist_spelling
from publishers.spotify.matching import normalize_recording_evidence_text as evidence_text
from publishers.spotify.matching import normalize_spotify_isrc as recording_code
from shared.discogs_api import DiscogsRateLimiter


DISCOGS_RELEASE_RELATION = "4a78823c-1c53-4176-a5f3-58026c76f2bc"
MUSICBRAINZ_ROOT = "https://musicbrainz.org/ws/2"
USER_AGENT = "DiscogsSpotifyTrackReview/1.0 (https://github.com/CoveMB/discogs-collection)"
MAX_REQUESTS = 64
MAX_BODY_BYTES = 4 * 1024 * 1024
DURATION_TOLERANCE_MS = 2000
MBID_PATTERN = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
BARCODE_PATTERN = re.compile(r"[0-9]{8}|[0-9]{12,14}")
VINYL_POSITION = re.compile(r"([A-Z])([1-9][0-9]*)?")
PLACEHOLDER = re.compile(r"(?:\[.*\]|\(?none\)?|n[./ ]?a\.?|unknown(?: label)?|not on label.*|[-?]+|0+)")
NULL_MBID = "00000000-0000-0000-0000-000000000000"


class IncompleteMusicBrainzEvidence(ValueError):
    """The lookup could not establish what the catalog contains."""


class UnverifiedPressing(ValueError):
    """The catalog evidence does not identify the exact source pressing."""


@dataclass(frozen=True)
class DiscogsTrackEvidence:
    position: str
    title: str
    artists: tuple[frozenset[str], ...]
    artist_ids: tuple[int, ...]
    duration_ms: int | None


@dataclass(frozen=True)
class RecordingEvidence:
    medium: int
    number: str
    recording_id: str
    isrcs: frozenset[str]
    duration_ms: int | None
    artist_ids: tuple[str, ...]
    reason: str = ""


@dataclass(frozen=True)
class PressingEvidence:
    release_id: str
    tracks: tuple[RecordingEvidence, ...]


def text_field(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def musicbrainz_id(value: object) -> str:
    return value if isinstance(value, str) and value != NULL_MBID and MBID_PATTERN.fullmatch(value) else ""


def duration_ms(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def discogs_duration_ms(value: object) -> int | None:
    parts = text_field(value).split(":")
    if len(parts) not in (2, 3) or any(not re.fullmatch(r"[0-9]{1,3}", part) for part in parts):
        return None
    seconds = 0
    for index, part in enumerate(parts):
        number = int(part)
        if index and (len(part) != 2 or number > 59):
            return None
        seconds = 60 * seconds + number
    return seconds * 1000 or None


def comparable_durations(*values: int) -> bool:
    return max(values) - min(values) <= DURATION_TOLERANCE_MS


class NoCatalogRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl) -> None:
        return None


class MusicBrainzClient:
    def __init__(self) -> None:
        self.remaining = MAX_REQUESTS
        self.stopped = ""
        self.rate_limiter = DiscogsRateLimiter(1)
        self.opener = build_opener(NoCatalogRedirects())

    def read(self, path: str, **query: str) -> Mapping[str, object] | None:
        if self.stopped:
            raise IncompleteMusicBrainzEvidence(self.stopped)
        request = Request(f"{MUSICBRAINZ_ROOT}/{path}?{urlencode({'fmt': 'json', **query})}", headers={
            "User-Agent": USER_AGENT, "Accept": "application/json",
        })
        for attempt in range(2):
            if not self.remaining:
                raise IncompleteMusicBrainzEvidence("The MusicBrainz request budget was exhausted.")
            self.remaining -= 1
            self.rate_limiter.wait_before_request()
            try:
                with self.opener.open(request, timeout=10) as response:
                    body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise IncompleteMusicBrainzEvidence("A MusicBrainz response exceeded the read limit.")
                payload = mapping(json.loads(body))
                if not payload:
                    raise IncompleteMusicBrainzEvidence("MusicBrainz returned a malformed response.")
                return payload
            except HTTPError as error:
                error.close()
                if error.code == 404:
                    return None
                reason = f"MusicBrainz returned HTTP {error.code}."
                if error.code == 429 or (error.headers and error.headers.get("Retry-After")):
                    self.stopped = reason
                if self.stopped or error.code not in {500, 502, 503, 504} or attempt:
                    raise IncompleteMusicBrainzEvidence(reason) from error
            except (OSError, http.client.HTTPException) as error:
                if attempt:
                    raise IncompleteMusicBrainzEvidence("A MusicBrainz request failed.") from error
            except (UnicodeError, json.JSONDecodeError) as error:
                raise IncompleteMusicBrainzEvidence("MusicBrainz returned invalid JSON.") from error
        raise AssertionError("MusicBrainz retry loop fell through")

    def pressing(
        self, release_id: str, discogs: Mapping[str, object], tracks: tuple[DiscogsTrackEvidence, ...],
    ) -> PressingEvidence:
        resource = f"https://www.discogs.com/release/{release_id}"
        links = self.read("url", resource=resource, inc="release-rels")
        if links is None:
            raise UnverifiedPressing("MusicBrainz has no relationship to the exact Discogs release URL.")
        if links.get("resource") != resource:
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned a different URL resource.")
        release_ids = {
            musicbrainz_id(mapping(link.get("release")).get("id"))
            for link in discogs_relations(links, "release", "backward")
        }
        if "" in release_ids:
            raise IncompleteMusicBrainzEvidence("A MusicBrainz release relationship has an invalid ID.")
        valid: list[PressingEvidence] = []
        reasons: list[str] = []
        for mbid in sorted(release_ids):
            release = self.read(f"release/{mbid}", inc="recordings+artist-credits+isrcs+labels+media+url-rels")
            if release is None or release.get("id") != mbid:
                raise IncompleteMusicBrainzEvidence("A linked MusicBrainz pressing could not be read by its ID.")
            try:
                valid.append(corroborate_pressing(mbid, release, resource, discogs, tracks))
            except UnverifiedPressing as error:
                reasons.append(str(error))
        if len(valid) == 1:
            return valid[0]
        if valid:
            raise UnverifiedPressing("More than one MusicBrainz release corroborates the exact pressing.")
        raise UnverifiedPressing(" ".join(dict.fromkeys(reasons)) or "No active Discogs pressing relationship was found.")

    def recordings_for_isrc(self, isrc: str) -> frozenset[str]:
        if not recording_code(isrc):
            raise ValueError("Invalid ISRC.")
        payload = self.read(f"isrc/{isrc}")
        if payload is None:
            return frozenset()
        recordings = payload.get("recordings")
        if payload.get("isrc") != isrc or not isinstance(recordings, list):
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned a malformed ISRC lookup.")
        ids = [musicbrainz_id(mapping(recording).get("id")) for recording in recordings]
        if not all(ids) or len(set(ids)) != len(ids):
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned malformed or repeated recording IDs for the ISRC.")
        return frozenset(ids)


def discogs_relations(
    payload: Mapping[str, object], target: str, direction: str,
) -> tuple[Mapping[str, object], ...]:
    relationships = payload.get("relations")
    if not isinstance(relationships, list):
        raise IncompleteMusicBrainzEvidence("MusicBrainz did not supply relationship evidence.")
    active: list[Mapping[str, object]] = []
    for item in relationships:
        relation = mapping(item)
        if not relation or not text_field(relation.get("type-id")):
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned a malformed relationship.")
        if relation["type-id"] != DISCOGS_RELEASE_RELATION:
            continue
        if relation.get("target-type") != target or relation.get("direction") != direction or (
            type(relation.get("ended")) is not bool
        ):
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned an invalid Discogs relationship.")
        if relation["ended"] is False:
            active.append(relation)
    return tuple(active)


def gtin(value: object) -> str:
    digits = re.sub(r"[\s-]", "", text_field(value))
    if not BARCODE_PATTERN.fullmatch(digits) or not digits.strip("0"):
        return ""
    checksum = sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(reversed(digits)))
    return digits.zfill(14) if checksum % 10 == 0 else ""


def published_label(name: object, catalogue: object) -> tuple[str, str] | None:
    pair = evidence_text(text_field(name)), evidence_text(text_field(catalogue))
    return pair if all(field and not PLACEHOLDER.fullmatch(field) for field in pair) else None


def corroborate_pressing(
    mbid: str, release: Mapping[str, object], resource: str, discogs: Mapping[str, object],
    tracks: tuple[DiscogsTrackEvidence, ...],
) -> PressingEvidence:
    resources = {text_field(mapping(link.get("url")).get("resource"))
                 for link in discogs_relations(release, "url", "forward")}
    if "" in resources:
        raise IncompleteMusicBrainzEvidence("A reciprocal MusicBrainz relationship has an invalid URL.")
    if resources != {resource}:
        raise UnverifiedPressing("MusicBrainz did not link back solely to the exact Discogs pressing.")
    dates = text_field(release.get("date"))
    year = discogs.get("year", 0)
    if not text_field(release.get("title")) or not isinstance(release.get("media"), list):
        raise IncompleteMusicBrainzEvidence("A linked MusicBrainz pressing has missing title or media evidence.")
    if (
        evidence_text(text_field(release.get("title"))) != evidence_text(text_field(discogs.get("title")))
        or type(year) is not int or year < 0 or year > 9999
        or (dates and not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2}){0,2}", dates))
        or (year and dates and str(year) != dates[:4])
    ):
        raise UnverifiedPressing("Discogs and MusicBrainz pressing titles or dates do not agree.")
    identifiers = discogs.get("identifiers")
    barcodes = {
        code for item in identifiers
        if mapping(item).get("type") == "Barcode" and (code := gtin(mapping(item).get("value")))
    } if isinstance(identifiers, list) else set()
    if gtin(release.get("barcode")) not in barcodes:
        raise UnverifiedPressing("The pressing has no common valid barcode.")
    discogs_labels = discogs.get("labels")
    mb_labels = release.get("label-info")
    if not isinstance(mb_labels, list):
        raise IncompleteMusicBrainzEvidence("A linked MusicBrainz pressing is missing label evidence.")
    source_pairs = {
        pair for label in discogs_labels
        if (pair := published_label(mapping(label).get("name"), mapping(label).get("catno")))
    } if isinstance(discogs_labels, list) else set()
    mb_pairs = {
        pair for entry in mb_labels
        if (pair := published_label(mapping(mapping(entry).get("label")).get("name"), mapping(entry).get("catalog-number")))
    }
    if not source_pairs.intersection(mb_pairs):
        raise UnverifiedPressing("The pressing has no common label and catalogue number.")
    return PressingEvidence(mbid, align_tracks(discogs.get("formats"), release.get("media"), tracks))


def align_tracks(
    formats: object, media: object, source_tracks: tuple[DiscogsTrackEvidence, ...],
) -> tuple[RecordingEvidence, ...]:
    if not isinstance(formats, list) or len(formats) != 1 or not isinstance(media, list) or not media:
        raise UnverifiedPressing("The pressing does not have a supported, unambiguous medium layout.")
    format_name = text_field(mapping(formats[0]).get("name"))
    if format_name not in {"CD", "Vinyl"} or text_field(mapping(formats[0]).get("qty")) != str(len(media)):
        raise UnverifiedPressing("Discogs and MusicBrainz do not identify the same physical media.")
    recordings: list[RecordingEvidence] = []
    last_side = -1
    next_side_number: int | None = 1
    for medium_number, raw_medium in enumerate(media, start=1):
        medium = mapping(raw_medium)
        medium_format = text_field(medium.get("format"))
        same_format = medium_format == format_name if format_name == "CD" else bool(
            re.fullmatch(r'(?:[0-9]+" )?Vinyl', medium_format)
        )
        if medium.get("pregap") is not None or medium.get("data-tracks") not in (None, []):
            raise UnverifiedPressing("MusicBrainz lists additional tracks outside the ordered audio tracklist.")
        tracks = medium.get("tracks")
        offset = medium.get("track-offset", 0)
        if (
            not isinstance(tracks, list) or type(medium.get("track-count")) is not int
            or type(offset) is not int or offset != 0
        ):
            raise IncompleteMusicBrainzEvidence("MusicBrainz returned an incomplete tracklist.")
        if not same_format or type(medium.get("position")) is not int or medium["position"] != medium_number:
            raise UnverifiedPressing("MusicBrainz medium order or format does not agree.")
        if not tracks or medium["track-count"] != len(tracks):
            raise UnverifiedPressing("MusicBrainz track counts do not agree.")
        for track_number, item in enumerate(tracks, start=1):
            if len(recordings) >= len(source_tracks):
                raise UnverifiedPressing("MusicBrainz has more tracks than the Discogs pressing.")
            source = source_tracks[len(recordings)]
            track = mapping(item)
            number = text_field(track.get("number"))
            if format_name == "Vinyl":
                vinyl = VINYL_POSITION.fullmatch(source.position)
                if not vinyl or source.position != number:
                    raise UnverifiedPressing("MusicBrainz does not preserve the printed vinyl track positions.")
                side = ord(vinyl[1]) - ord("A")
                if side // 2 + 1 != medium_number or side < last_side:
                    raise UnverifiedPressing("The vinyl sides do not follow the printed medium order.")
                if side != last_side:
                    last_side, next_side_number = side, 1
                if int(vinyl[2] or "1") != next_side_number:
                    raise UnverifiedPressing("The vinyl positions are mixed, repeated or not sequential.")
                next_side_number = int(vinyl[2]) + 1 if vinyl[2] else None
            else:
                expected = str(track_number)
                printed = expected if len(media) == 1 else f"{medium_number}-{expected}"
                if source.position != printed or number not in {expected, printed}:
                    raise UnverifiedPressing("The numbered positions cannot be tied to the printed medium and track.")
            if type(track.get("position")) is not int or track["position"] != track_number:
                raise UnverifiedPressing("A MusicBrainz track does not follow the pressing order.")
            recordings.append(corroborate_recording(source, track, medium_number, number))
    if len(recordings) != len(source_tracks):
        raise UnverifiedPressing("MusicBrainz has fewer tracks than the Discogs pressing.")
    return tuple(recordings)


def artist_ids(source: DiscogsTrackEvidence, credits: object) -> tuple[str, ...] | None:
    if not isinstance(credits, list) or len(credits) != len(source.artists) or not source.artists:
        return None
    ids: list[str] = []
    for known_names, item in zip(source.artists, credits, strict=True):
        credit = mapping(item)
        artist = mapping(credit.get("artist"))
        credited_name = artist_spelling(text_field(credit.get("name")))
        canonical_name = artist_spelling(text_field(artist.get("name")))
        if credited_name not in known_names or canonical_name not in known_names:
            return None
        mbid = musicbrainz_id(artist.get("id"))
        if "id" in artist and not mbid:
            return None
        ids.append(mbid)
    return tuple(ids)


def conflicting_live_disambiguation(title: str, comment: str) -> bool:
    # MusicBrainz puts a live performance marker in disambiguation, outside the recording title.
    qualifier = evidence_text(comment)
    if qualifier.partition(",")[0].strip() != "live":
        return False
    return not title.endswith((f" ({qualifier})", f" [{qualifier}]", f" - {qualifier}"))


def corroborate_recording(
    source: DiscogsTrackEvidence, track: Mapping[str, object], medium: int, number: str,
) -> RecordingEvidence:
    title = evidence_text(source.title)
    if evidence_text(text_field(track.get("title"))) != title:
        raise UnverifiedPressing("The ordered track titles do not match the exact pressing.")
    track_artists = artist_ids(source, track.get("artist-credit"))
    if track_artists is None:
        raise UnverifiedPressing("The ordered track credits do not match the exact pressing.")
    recording = mapping(track.get("recording"))
    recording_id = musicbrainz_id(recording.get("id"))
    comment = recording.get("disambiguation", "")
    linked_artists = artist_ids(source, recording.get("artist-credit"))
    codes = recording.get("isrcs")
    isrcs = frozenset(code for value in codes if (code := recording_code(value))) if isinstance(codes, list) else frozenset()
    length = duration_ms(track.get("length"))
    reason = ""
    if (
        not recording_id or evidence_text(text_field(recording.get("title"))) != title
        or linked_artists is None
        or any(left and right and left != right for left, right in zip(track_artists, linked_artists, strict=True))
    ):
        reason = "The linked MusicBrainz recording has conflicting title, version or credit evidence."
    elif not isinstance(comment, str):
        reason = "The linked MusicBrainz recording has malformed disambiguation."
    elif conflicting_live_disambiguation(title, comment):
        reason = "The MusicBrainz recording's live disambiguation does not match the printed title."
    elif not isinstance(codes, list) or len(isrcs) != 1 or any(not recording_code(value) for value in codes):
        reason = "The linked MusicBrainz recording does not establish a unique valid ISRC."
    elif length is None or source.duration_ms is None or not comparable_durations(length, source.duration_ms):
        reason = "The printed track and MusicBrainz pressing durations do not agree."
    known_artist_ids = tuple(
        left or right for left, right in zip(track_artists, linked_artists, strict=True)
    ) if linked_artists is not None else track_artists
    return RecordingEvidence(medium, number, recording_id, isrcs, length, known_artist_ids, reason)


def conflicting_recording_claims(
    sources: tuple[DiscogsTrackEvidence, ...], recordings: tuple[RecordingEvidence, ...],
) -> dict[int, str]:
    claims: dict[tuple[str, str], list[int]] = {}
    for index, recording in enumerate(recordings):
        for code in recording.isrcs:
            claims.setdefault(("ISRC", code), []).append(index)
        if recording.recording_id:
            claims.setdefault(("MusicBrainz recording ID", recording.recording_id), []).append(index)

    conflicts: dict[int, str] = {}
    for (kind, _), indices in claims.items():
        if len(indices) < 2:
            continue
        # Printed position identifies an occurrence. Matching evidence can identify the same recording twice.
        evidence = {
            (
                evidence_text(sources[index].title), sources[index].artists, sources[index].artist_ids,
                sources[index].duration_ms, recordings[index].duration_ms, recordings[index].artist_ids,
                recordings[index].recording_id, recordings[index].isrcs,
            )
            for index in indices
        }
        if len(evidence) != 1 or any(recordings[index].reason for index in indices):
            for index in indices:
                conflicts.setdefault(
                    index, f"The {kind} is claimed by more than one printed source position with conflicting or incomplete evidence."
                )
    return conflicts
