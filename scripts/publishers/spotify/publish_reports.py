"""Report formatting for Spotify playlist publishing."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from publishers.spotify.matching import (
    AMBIGUOUS,
    ERROR,
    MATCHED,
    UNMATCHED,
    PlaylistTrack,
    SpotifyTrackCandidate,
    TrackMatchDecision,
    bounded_damerau_levenshtein_distance,
    build_spotify_track_search_query,
    normalize_music_text,
    normalized_candidate_artist_set,
    normalized_source_artist_set,
    source_artist_matches_candidate,
)
from publishers.spotify.publish_types import (
    ADDED,
    ALREADY_PRESENT,
    DUPLICATE_IN_SOURCE,
    INCLUDED,
    MATCH_SOURCE_CACHE,
    MATCH_SOURCE_SEARCH,
    WOULD_ADD,
    WOULD_INCLUDE,
    FinalPlaylistItem,
    PlaylistPublishContext,
    PlaylistPublishDecision,
    SpotifyDryRunSummary,
    SpotifyPublishSummary,
)
from shared.reports import format_report_section, format_report_title, write_text_report
from shared.text import display_report_value


def build_publish_summary(
    decisions: tuple[PlaylistPublishDecision, ...],
    final_items: tuple[FinalPlaylistItem, ...],
    playlist_contexts: tuple[PlaylistPublishContext, ...],
    report_path: Path,
    apply: bool,
    publisher_sync_mode: str,
    cache_hit_count: int,
    search_count: int,
    searched_row_count: int = 0,
    run_status: str = "complete",
) -> SpotifyPublishSummary:
    playlist_names = {context.target_playlist_name for context in playlist_contexts}
    return SpotifyPublishSummary(
        playlist_count=len(playlist_names),
        track_count=len(decisions),
        run_status=run_status,
        cache_hit_count=cache_hit_count,
        search_count=search_count,
        searched_row_count=searched_row_count,
        matched_count=sum(1 for decision in decisions if decision.spotify_uri),
        ambiguous_count=sum(1 for decision in decisions if decision.status == AMBIGUOUS),
        unmatched_count=sum(1 for decision in decisions if decision.status == UNMATCHED),
        error_count=sum(1 for decision in decisions if decision.status == ERROR),
        already_present_count=sum(1 for decision in decisions if decision.status == ALREADY_PRESENT),
        would_add_count=sum(1 for decision in decisions if decision.status == WOULD_ADD),
        added_count=sum(1 for decision in decisions if decision.status == ADDED),
        would_include_count=sum(1 for decision in decisions if decision.status == WOULD_INCLUDE),
        included_count=sum(1 for decision in decisions if decision.status == INCLUDED),
        duplicate_in_source_count=sum(1 for decision in decisions if decision.status == DUPLICATE_IN_SOURCE),
        report_path=report_path,
        apply=apply,
        publisher_sync_mode=publisher_sync_mode,
        decisions=decisions,
        final_items=final_items,
        playlist_contexts=playlist_contexts,
    )


def write_publish_report(path: Path, summary: SpotifyPublishSummary) -> None:
    title = "Spotify playlist publish report" if summary.apply else "Spotify playlist publish dry-run report"
    lines = format_report_title(title)
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Run status: {summary.run_status}",
                f"- Publisher sync mode: {summary.publisher_sync_mode}",
                f"- Playlists: {summary.playlist_count}",
                f"- Tracks: {summary.track_count}",
                f"- Cache hits: {summary.cache_hit_count}",
                f"- Spotify searches: {summary.search_count}",
                f"- Rows searched with ladder: {summary.searched_row_count}",
                f"- Cached matched tracks: {cached_matched_publish_decision_count(summary.decisions)}",
                f"- Cached ambiguous tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_CACHE, {AMBIGUOUS})}",
                f"- Cached unmatched tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_CACHE, {UNMATCHED})}",
                f"- Searched matched tracks: {searched_matched_publish_decision_count(summary.decisions)}",
                f"- Searched ambiguous tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {AMBIGUOUS})}",
                f"- Searched unmatched tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {UNMATCHED})}",
                f"- Searched error tracks: {count_publish_decisions(summary.decisions, MATCH_SOURCE_SEARCH, {ERROR})}",
                f"- Matched tracks: {summary.matched_count}",
                f"- Already-present tracks: {summary.already_present_count}",
                f"- Would add tracks: {summary.would_add_count}",
                f"- Added tracks: {summary.added_count}",
                f"- Tracks that would be included in replacement: {summary.would_include_count}",
                f"- Tracks included in replacement: {summary.included_count}",
                f"- Duplicate source tracks skipped: {summary.duplicate_in_source_count}",
                f"- Ambiguous tracks: {summary.ambiguous_count}",
                f"- Unmatched tracks: {summary.unmatched_count}",
                f"- Search errors: {summary.error_count}",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Playlist checks",
            [f"- {context.info_message}" for context in summary.playlist_contexts] or ["- None"],
        )
    )
    lines.extend(format_report_section("Already-present tracks", format_publish_decisions(summary.decisions, {ALREADY_PRESENT})))
    lines.extend(format_report_section("Tracks that would be added", format_publish_decisions(summary.decisions, {WOULD_ADD})))
    lines.extend(format_report_section("Tracks added", format_publish_decisions(summary.decisions, {ADDED})))
    lines.extend(format_report_section("Tracks that would be included in replacement", format_publish_decisions(summary.decisions, {WOULD_INCLUDE})))
    lines.extend(format_report_section("Tracks included in replacement", format_publish_decisions(summary.decisions, {INCLUDED})))
    lines.extend(format_report_section("Duplicate source tracks skipped", format_publish_decisions(summary.decisions, {DUPLICATE_IN_SOURCE})))
    lines.extend(
        format_report_section(
            "Ambiguous tracks needing review",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == AMBIGUOUS),
        )
    )
    lines.extend(
        format_report_section(
            "Unmatched tracks needing review",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == UNMATCHED),
        )
    )
    lines.extend(
        format_report_section(
            "Search errors",
            flatten_report_details(format_publish_review_details(decision) for decision in summary.decisions if decision.status == ERROR),
        )
    )
    lines.extend(format_report_section("Final planned playlist state", [format_final_playlist_item(item) for item in summary.final_items] or ["- None"]))
    lines.extend(format_report_section("Track publish decisions", [format_publish_decision(decision) for decision in summary.decisions] or ["- None"]))
    write_text_report(path, lines)


def count_publish_decisions(
    decisions: Sequence[PlaylistPublishDecision],
    match_source: str,
    statuses: set[str],
) -> int:
    return sum(1 for decision in decisions if decision.match_source == match_source and decision.status in statuses)


def cached_matched_publish_decision_count(decisions: Sequence[PlaylistPublishDecision]) -> int:
    return sum(1 for decision in decisions if decision.match_source == MATCH_SOURCE_CACHE and bool(decision.spotify_uri))


def searched_matched_publish_decision_count(decisions: Sequence[PlaylistPublishDecision]) -> int:
    return sum(1 for decision in decisions if decision.match_source == MATCH_SOURCE_SEARCH and bool(decision.spotify_uri))


def format_publish_decisions(decisions: Sequence[PlaylistPublishDecision], statuses: set[str]) -> list[str]:
    return [format_publish_decision(decision) for decision in decisions if decision.status in statuses] or ["- None"]


def format_publish_decision(decision: PlaylistPublishDecision) -> str:
    track = decision.track
    return (
        f"- {decision.target_playlist_name} | {track.release_id} | {track.track_number} | "
        f"{track.artist_name} | {track.track_name} | {decision.status} | "
        f"{decision.spotify_uri or 'no Spotify URI'} | {display_report_value(decision.reason)}"
    )


def format_publish_review_details(decision: PlaylistPublishDecision) -> list[str]:
    lines = [
        f"- {decision.target_playlist_name} | {format_track_context(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]
    search_queries = decision.search_queries or (build_spotify_track_search_query(decision.track),)
    lines.append("  Search queries:")
    lines.extend(f"    - {display_report_value(query)}" for query in search_queries)
    if decision.status == ERROR:
        return lines
    if decision.status == AMBIGUOUS:
        if decision.review_candidates:
            lines.append("  Matching Spotify candidates:")
            lines.extend(f"    - {format_spotify_candidate(candidate)}" for candidate in decision.review_candidates)
        else:
            lines.append("  Matching Spotify candidates: none recorded")
        return lines
    append_unmatched_candidate_diagnostics(lines, decision.track, decision.review_candidates)
    return lines


def format_final_playlist_item(item: FinalPlaylistItem) -> str:
    return (
        f"- {item.playlist_name} | {item.position} | {item.status} | "
        f"{format_artist_names(item.artist_names)} | {display_report_value(item.track_name)} | "
        f"{display_report_value(item.album_name)} | {item.spotify_uri or 'no Spotify URI'}"
    )


def format_artist_names(artist_names: Sequence[str]) -> str:
    return display_report_value(", ".join(artist_names))


def build_summary(decisions: tuple[TrackMatchDecision, ...], report_path: Path) -> SpotifyDryRunSummary:
    playlist_names = {decision.track.playlist_name for decision in decisions}
    return SpotifyDryRunSummary(
        playlist_count=len(playlist_names),
        track_count=len(decisions),
        matched_count=sum(1 for decision in decisions if decision.status == MATCHED),
        ambiguous_count=sum(1 for decision in decisions if decision.status == AMBIGUOUS),
        unmatched_count=sum(1 for decision in decisions if decision.status == UNMATCHED),
        error_count=sum(1 for decision in decisions if decision.status == ERROR),
        report_path=report_path,
        decisions=decisions,
    )


def write_dry_run_report(path: Path, summary: SpotifyDryRunSummary) -> None:
    lines = format_report_title("Spotify playlist dry-run report")
    lines.extend(
        format_report_section(
            "Summary",
            [
                f"- Playlists: {summary.playlist_count}",
                f"- Tracks: {summary.track_count}",
                f"- Matched tracks: {summary.matched_count}",
                f"- Ambiguous tracks: {summary.ambiguous_count}",
                f"- Unmatched tracks: {summary.unmatched_count}",
                f"- Search errors: {summary.error_count}",
            ],
        )
    )
    lines.extend(
        format_report_section(
            "Ambiguous tracks needing review",
            flatten_report_details(format_ambiguous_track_details(decision) for decision in summary.decisions if decision.status == AMBIGUOUS),
        )
    )
    lines.extend(
        format_report_section(
            "Unmatched tracks needing review",
            flatten_report_details(format_unmatched_track_details(decision) for decision in summary.decisions if decision.status == UNMATCHED),
        )
    )
    lines.extend(
        format_report_section(
            "Search errors",
            flatten_report_details(format_search_error_details(decision) for decision in summary.decisions if decision.status == ERROR),
        )
    )
    lines.extend(format_report_section("Track match decisions", [format_match_decision(decision) for decision in summary.decisions] or ["- None"]))
    write_text_report(path, lines)


def format_match_decision(decision: TrackMatchDecision) -> str:
    track = decision.track
    return (
        f"- {track.playlist_name} | {track.release_id} | {track.track_number} | "
        f"{track.artist_name} | {track.track_name} | {decision.status} | "
        f"{decision.spotify_uri or 'no Spotify URI'} | {display_report_value(decision.reason)}"
    )


def flatten_report_details(detail_groups: Iterable[list[str]]) -> list[str]:
    lines: list[str] = []
    for detail_group in detail_groups:
        if lines:
            lines.append("")
        lines.extend(detail_group)
    return lines or ["- None"]


def format_ambiguous_track_details(decision: TrackMatchDecision) -> list[str]:
    lines = [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]
    if decision.review_candidates:
        lines.append("  Matching Spotify candidates:")
        lines.extend(f"    - {format_spotify_candidate(candidate)}" for candidate in decision.review_candidates)
    else:
        lines.append("  Matching Spotify candidates: none recorded")
    return lines


def format_unmatched_track_details(decision: TrackMatchDecision) -> list[str]:
    lines = [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Why: {display_report_value(decision.reason)}",
    ]
    append_unmatched_candidate_diagnostics(lines, decision.track, decision.review_candidates)
    return lines


def format_search_error_details(decision: TrackMatchDecision) -> list[str]:
    return [
        f"- {format_track_context(decision.track)}",
        f"  Search query: {format_search_query(decision.track)}",
        f"  Error: {display_report_value(decision.reason)}",
    ]


def format_track_context(track: PlaylistTrack) -> str:
    return (
        f"{display_report_value(track.playlist_name)} | {display_report_value(track.release_id)} | "
        f"{display_report_value(track.track_number)} | {display_report_value(track.artist_name)} | "
        f"{display_report_value(track.track_name)} | {display_report_value(track.album_name)}"
    )


def format_search_query(track: PlaylistTrack) -> str:
    return display_report_value(build_spotify_track_search_query(track))


def format_spotify_candidate(candidate: SpotifyTrackCandidate) -> str:
    return (
        f"{candidate.uri} | {format_spotify_artists(candidate)} | "
        f"{display_report_value(candidate.name)} | {display_report_value(candidate.album_name)}"
    )


def format_spotify_artists(candidate: SpotifyTrackCandidate) -> str:
    return display_report_value(", ".join(candidate.artists))


def append_unmatched_candidate_diagnostics(
    lines: list[str],
    track: PlaylistTrack,
    candidates: Sequence[SpotifyTrackCandidate],
) -> None:
    if not candidates:
        lines.append("  Spotify returned 0 candidates.")
        lines.append("  Diagnostic: no Spotify candidates were returned by any query.")
        return

    candidate = best_diagnostic_candidate(track, candidates)
    lines.append(f"  Spotify returned {len(candidates)} candidate(s).")
    lines.append(f"  Best diagnostic candidate: {format_spotify_candidate(candidate)}")
    lines.append(f"  Diagnostic: {format_candidate_diagnostic_summary(track, candidate)}")
    lines.append("  Comparison:")
    lines.extend(format_candidate_comparison(track, candidate))


def best_diagnostic_candidate(
    track: PlaylistTrack,
    candidates: Sequence[SpotifyTrackCandidate],
) -> SpotifyTrackCandidate:
    return min(candidates, key=lambda candidate: diagnostic_candidate_sort_key(track, candidate))


def diagnostic_candidate_sort_key(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> tuple[int, int, int, int, int, str, str, tuple[str, ...], str]:
    title_matches = normalized_nonempty_values_match(track.track_name, candidate.name)
    artist_set_matches = normalized_artist_sets_match(track.artist_name, candidate.artists)
    album_matches = normalized_nonempty_values_match(track.album_name, candidate.album_name)
    partial_artist_match = source_artist_matches_candidate(track.artist_name, candidate.artists)
    exact_identity_field_count = sum((title_matches, artist_set_matches, album_matches))
    return (
        -exact_identity_field_count,
        -int(artist_set_matches),
        -int(album_matches),
        -int(partial_artist_match),
        candidate_title_edit_distance(track, candidate),
        normalize_music_text(candidate.name),
        normalize_music_text(candidate.album_name),
        tuple(sorted(normalized_candidate_artist_set(candidate.artists))),
        candidate.uri,
    )


def candidate_title_edit_distance(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> int:
    source_title = normalize_music_text(track.track_name)
    candidate_title = normalize_music_text(candidate.name)
    return bounded_damerau_levenshtein_distance(
        source_title,
        candidate_title,
        maximum_distance=max(len(source_title), len(candidate_title)),
    )


def normalized_nonempty_values_match(left: str, right: str) -> bool:
    left_key = normalize_music_text(left)
    return bool(left_key) and left_key == normalize_music_text(right)


def normalized_artist_sets_match(
    source_artist_name: str,
    candidate_artists: tuple[str, ...],
) -> bool:
    source_artist_set = normalized_source_artist_set(source_artist_name)
    candidate_artist_set = normalized_candidate_artist_set(candidate_artists)
    return bool(source_artist_set) and source_artist_set == candidate_artist_set


def format_candidate_diagnostic_summary(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> str:
    title_evidence = format_title_diagnostic_evidence(track, candidate)
    artist_evidence = format_artist_diagnostic_evidence(track, candidate)
    album_evidence = format_album_diagnostic_evidence(track, candidate)
    return f"{title_evidence}; {artist_evidence}; {album_evidence}."


def format_title_diagnostic_evidence(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> str:
    source_title = normalize_music_text(track.track_name)
    candidate_title = normalize_music_text(candidate.name)
    if not source_title or not candidate_title:
        return "title comparison unavailable"
    if source_title == candidate_title:
        return "title matches"
    distance = candidate_title_edit_distance(track, candidate)
    edit_label = "edit" if distance == 1 else "edits"
    return f"title differs by {distance} {edit_label}"


def format_artist_diagnostic_evidence(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> str:
    source_artist_set = normalized_source_artist_set(track.artist_name)
    candidate_artist_set = normalized_candidate_artist_set(candidate.artists)
    if not source_artist_set or not candidate_artist_set:
        return "artist comparison unavailable"
    if source_artist_set == candidate_artist_set:
        return "artist set matches"
    if source_artist_matches_candidate(track.artist_name, candidate.artists):
        return "at least one source artist matches"
    return "artist differs"


def format_album_diagnostic_evidence(
    track: PlaylistTrack,
    candidate: SpotifyTrackCandidate,
) -> str:
    source_album = normalize_music_text(track.album_name)
    candidate_album = normalize_music_text(candidate.album_name)
    if not source_album or not candidate_album:
        return "album comparison unavailable"
    return "album matches" if source_album == candidate_album else "album differs"


def format_candidate_comparison(track: PlaylistTrack, candidate: SpotifyTrackCandidate) -> list[str]:
    artist_set_matches = normalized_artist_sets_match(track.artist_name, candidate.artists)
    partial_artist_match = source_artist_matches_candidate(track.artist_name, candidate.artists)
    artist_status = "matches exactly" if artist_set_matches else "partial match" if partial_artist_match else "different"
    return [
        format_field_comparison_status(
            "Track name",
            track.track_name,
            candidate.name,
            "matches" if normalized_nonempty_values_match(track.track_name, candidate.name) else "different",
        ),
        f"    Title edit distance: {candidate_title_edit_distance(track, candidate)}",
        format_field_comparison_status(
            "Artist",
            track.artist_name,
            ", ".join(candidate.artists),
            artist_status,
        ),
        format_field_comparison_status(
            "Album",
            track.album_name,
            candidate.album_name,
            "matches" if normalized_nonempty_values_match(track.album_name, candidate.album_name) else "different",
        ),
    ]


def format_field_comparison_status(
    label: str,
    discogs_value: str,
    spotify_value: str,
    status: str,
) -> str:
    return (
        f"    {label}: {status} "
        f"(Discogs: {display_report_value(discogs_value)}; Spotify: {display_report_value(spotify_value)})"
    )


def format_field_comparison(
    label: str,
    discogs_value: str,
    spotify_value: str,
    matches: bool,
) -> str:
    return format_field_comparison_status(
        label,
        discogs_value,
        spotify_value,
        "matches" if matches else "different",
    )
