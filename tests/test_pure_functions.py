"""
Tests for pure functions in dvd-dashboard.py.

These tests require no mocking - they test pure logic functions that
have no external dependencies.

See TESTING.md for the full testing roadmap.
"""
import pytest
import sys
import os
import importlib.util

# Import the dashboard module (has hyphen in filename)
spec = importlib.util.spec_from_file_location(
    "dvd_dashboard",
    os.path.join(os.path.dirname(__file__), '..', 'web', 'dvd-dashboard.py')
)
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


# =============================================================================
# Tests for _format_bytes()
# =============================================================================

class TestFormatBytes:
    """Tests for _format_bytes() byte formatting function."""

    def test_bytes(self):
        """Test bytes (< 1KB)."""
        assert dashboard._format_bytes(0) == "0.0 B"
        assert dashboard._format_bytes(1) == "1.0 B"
        assert dashboard._format_bytes(512) == "512.0 B"
        assert dashboard._format_bytes(1023) == "1023.0 B"

    def test_kilobytes(self):
        """Test kilobyte conversion."""
        assert dashboard._format_bytes(1024) == "1.0 KB"
        assert dashboard._format_bytes(1536) == "1.5 KB"
        assert dashboard._format_bytes(10240) == "10.0 KB"

    def test_megabytes(self):
        """Test megabyte conversion."""
        assert dashboard._format_bytes(1024 * 1024) == "1.0 MB"
        assert dashboard._format_bytes(1024 * 1024 * 500) == "500.0 MB"

    def test_gigabytes(self):
        """Test gigabyte conversion."""
        assert dashboard._format_bytes(1024 ** 3) == "1.0 GB"
        assert dashboard._format_bytes(1024 ** 3 * 4.7) == "4.7 GB"

    def test_terabytes(self):
        """Test terabyte conversion."""
        assert dashboard._format_bytes(1024 ** 4) == "1.0 TB"
        assert dashboard._format_bytes(1024 ** 4 * 2) == "2.0 TB"

    def test_petabytes(self):
        """Test petabyte conversion (overflow case)."""
        assert dashboard._format_bytes(1024 ** 5) == "1.0 PB"

    def test_negative_values(self):
        """Test negative byte values (edge case)."""
        # Function uses abs() so negative values work
        assert dashboard._format_bytes(-1024) == "-1.0 KB"


# =============================================================================
# Tests for is_generic_title()
# =============================================================================

class TestIsGenericTitle:
    """Tests for is_generic_title() function."""

    def test_empty_title(self):
        """Empty or None titles are generic."""
        assert dashboard.is_generic_title("") is True
        assert dashboard.is_generic_title(None) is True

    def test_dvd_timestamp_format(self):
        """DVD_YYYYMMDD_HHMMSS format is generic."""
        assert dashboard.is_generic_title("DVD_20240101_120000") is True
        assert dashboard.is_generic_title("DVD_20231225_235959") is True

    def test_common_generic_names(self):
        """Common generic DVD names are detected."""
        assert dashboard.is_generic_title("DVD_VIDEO") is True
        assert dashboard.is_generic_title("DVDVIDEO") is True
        assert dashboard.is_generic_title("VIDEO_TS") is True
        assert dashboard.is_generic_title("MYDVD") is True
        assert dashboard.is_generic_title("DVD") is True

    def test_disc_patterns(self):
        """DISC/DISK patterns are generic."""
        assert dashboard.is_generic_title("DISC") is True
        assert dashboard.is_generic_title("DISC1") is True
        assert dashboard.is_generic_title("DISC2") is True
        assert dashboard.is_generic_title("DISK") is True
        assert dashboard.is_generic_title("DISK1") is True

    def test_case_insensitive(self):
        """Generic detection is case-insensitive."""
        assert dashboard.is_generic_title("dvd_video") is True
        assert dashboard.is_generic_title("Dvd_Video") is True
        assert dashboard.is_generic_title("disc1") is True

    def test_short_titles_generic(self):
        """Titles 3 chars or less are considered generic."""
        assert dashboard.is_generic_title("A") is True
        assert dashboard.is_generic_title("AB") is True
        assert dashboard.is_generic_title("ABC") is True

    def test_valid_movie_titles(self):
        """Real movie titles are not generic."""
        assert dashboard.is_generic_title("The_Matrix") is False
        assert dashboard.is_generic_title("Inception") is False
        assert dashboard.is_generic_title("Star_Wars") is False
        assert dashboard.is_generic_title("JAWS") is False  # 4 chars, valid

    def test_edge_cases(self):
        """Edge cases for generic detection."""
        # Contains DVD but not exact match
        assert dashboard.is_generic_title("MY_DVD_COLLECTION") is False
        # Starts with DISC but has more content
        assert dashboard.is_generic_title("DISC_SPECIAL_EDITION") is False


# =============================================================================
# Tests for sanitize_filename()
# =============================================================================

class TestSanitizeFilename:
    """Tests for sanitize_filename() function."""

    def test_basic_sanitization(self):
        """Basic characters pass through unchanged."""
        assert dashboard.sanitize_filename("The_Matrix") == "The_Matrix"
        assert dashboard.sanitize_filename("test-file") == "test-file"
        assert dashboard.sanitize_filename("movie.mkv") == "movie.mkv"

    def test_special_chars_replaced(self):
        """Special characters are replaced with underscore."""
        assert dashboard.sanitize_filename("Movie: Part 1") == "Movie_Part_1"
        assert dashboard.sanitize_filename("File/Name") == "File_Name"
        assert dashboard.sanitize_filename("Test*File") == "Test_File"

    def test_spaces_replaced(self):
        """Spaces are replaced with underscores."""
        assert dashboard.sanitize_filename("The Matrix") == "The_Matrix"
        assert dashboard.sanitize_filename("A B C") == "A_B_C"

    def test_multiple_underscores_collapsed(self):
        """Multiple consecutive underscores are collapsed."""
        assert dashboard.sanitize_filename("A___B") == "A_B"
        assert dashboard.sanitize_filename("Test:  File") == "Test_File"

    def test_leading_trailing_underscores_stripped(self):
        """Leading and trailing underscores are removed."""
        assert dashboard.sanitize_filename("_test_") == "test"
        assert dashboard.sanitize_filename("__hello__") == "hello"

    def test_parentheses_handling(self):
        """Parentheses are replaced."""
        assert dashboard.sanitize_filename("Movie (2024)") == "Movie_2024"

    def test_preserves_alphanumeric(self):
        """Alphanumeric characters, dots, hyphens, underscores preserved."""
        assert dashboard.sanitize_filename("File-2024.test_v1") == "File-2024.test_v1"


# =============================================================================
# Tests for generate_plex_filename()
# =============================================================================

class TestGeneratePlexFilename:
    """Tests for generate_plex_filename() function."""

    def test_basic_title_with_year(self):
        """Standard movie title with year."""
        result = dashboard.generate_plex_filename("The_Matrix", "1999", "mkv")
        assert result == "The Matrix (1999).mkv"

    def test_title_without_year(self):
        """Title without year omits parentheses."""
        result = dashboard.generate_plex_filename("The_Matrix", "", "mkv")
        assert result == "The Matrix.mkv"

    def test_none_year(self):
        """None year is handled gracefully."""
        result = dashboard.generate_plex_filename("The_Matrix", None, "mkv")
        assert result == "The Matrix.mkv"

    def test_invalid_year_format(self):
        """Non-4-digit years are omitted."""
        result = dashboard.generate_plex_filename("Test", "99", "mkv")
        assert result == "Test.mkv"
        result = dashboard.generate_plex_filename("Test", "19999", "mkv")
        assert result == "Test.mkv"

    def test_underscores_replaced_with_spaces(self):
        """Underscores in title become spaces."""
        result = dashboard.generate_plex_filename("Star_Wars_Episode_IV", "1977", "mkv")
        assert result == "Star Wars Episode Iv (1977).mkv"

    def test_title_casing(self):
        """Each word is capitalized."""
        result = dashboard.generate_plex_filename("the_matrix", "1999", "mkv")
        assert result == "The Matrix (1999).mkv"

    def test_different_extensions(self):
        """Different file extensions work correctly."""
        assert dashboard.generate_plex_filename("Test", "2024", "mp4") == "Test (2024).mp4"
        assert dashboard.generate_plex_filename("Test", "2024", "m4v") == "Test (2024).m4v"

    def test_year_as_integer(self):
        """Year passed as integer is converted to string."""
        result = dashboard.generate_plex_filename("Test", 2024, "mkv")
        assert result == "Test (2024).mkv"


# =============================================================================
# Tests for parse_cluster_peers()
# =============================================================================

class TestParseClusterPeers:
    """Tests for parse_cluster_peers() function."""

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert dashboard.parse_cluster_peers("") == []

    def test_none_input(self):
        """None input returns empty list."""
        assert dashboard.parse_cluster_peers(None) == []

    def test_single_peer(self):
        """Single peer is parsed correctly."""
        result = dashboard.parse_cluster_peers("plex:192.168.1.50:5000")
        assert len(result) == 1
        assert result[0] == {"name": "plex", "host": "192.168.1.50", "port": 5000}

    def test_multiple_peers(self):
        """Multiple peers are parsed correctly."""
        result = dashboard.parse_cluster_peers("plex:192.168.1.50:5000 cart:192.168.1.34:5000")
        assert len(result) == 2
        assert result[0] == {"name": "plex", "host": "192.168.1.50", "port": 5000}
        assert result[1] == {"name": "cart", "host": "192.168.1.34", "port": 5000}

    def test_invalid_format_skipped(self):
        """Invalid entries (not enough parts) are skipped."""
        result = dashboard.parse_cluster_peers("plex:192.168.1.50:5000 invalid:missing")
        assert len(result) == 1
        assert result[0]["name"] == "plex"

    def test_extra_colons_handled(self):
        """Entries with extra colons use first three parts."""
        result = dashboard.parse_cluster_peers("peer:host:5000:extra")
        assert len(result) == 1
        assert result[0] == {"name": "peer", "host": "host", "port": 5000}

    def test_port_conversion(self):
        """Port is converted to integer."""
        result = dashboard.parse_cluster_peers("test:localhost:8080")
        assert result[0]["port"] == 8080
        assert isinstance(result[0]["port"], int)


# =============================================================================
# Tests for get_restart_recommendations()
# =============================================================================

class TestGetRestartRecommendations:
    """Tests for get_restart_recommendations() function."""

    def test_empty_changes(self):
        """No changes returns empty list."""
        assert dashboard.get_restart_recommendations([]) == []

    def test_encoder_settings(self):
        """Encoder settings recommend encoder restart."""
        result = dashboard.get_restart_recommendations(["HANDBRAKE_PRESET"])
        assert len(result) == 1
        assert result[0] == {"name": "dvd-encoder", "type": "timer"}

    def test_transfer_settings(self):
        """Transfer settings recommend transfer restart."""
        result = dashboard.get_restart_recommendations(["NAS_HOST"])
        assert len(result) == 1
        assert result[0] == {"name": "dvd-transfer", "type": "timer"}

    def test_cluster_settings(self):
        """Cluster settings recommend both encoder and transfer restart."""
        result = dashboard.get_restart_recommendations(["CLUSTER_ENABLED"])
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "dvd-encoder" in names
        assert "dvd-transfer" in names

    def test_staging_dir_affects_all(self):
        """STAGING_DIR affects encoder, transfer, and dashboard."""
        result = dashboard.get_restart_recommendations(["STAGING_DIR"])
        assert len(result) == 3
        names = [r["name"] for r in result]
        assert "dvd-encoder" in names
        assert "dvd-transfer" in names
        assert "dvd-dashboard" in names

    def test_deduplication(self):
        """Same service not recommended twice."""
        result = dashboard.get_restart_recommendations(["HANDBRAKE_PRESET", "HANDBRAKE_QUALITY"])
        # Both are encoder settings, should only appear once
        assert len(result) == 1
        assert result[0]["name"] == "dvd-encoder"

    def test_multiple_patterns(self):
        """Multiple different patterns each add their services."""
        result = dashboard.get_restart_recommendations(["HANDBRAKE_PRESET", "NAS_HOST"])
        assert len(result) == 2
        names = [r["name"] for r in result]
        assert "dvd-encoder" in names
        assert "dvd-transfer" in names

    def test_preview_settings(self):
        """Preview generation settings affect encoder."""
        result = dashboard.get_restart_recommendations(["GENERATE_PREVIEWS"])
        assert len(result) == 1
        assert result[0]["name"] == "dvd-encoder"

    def test_unknown_key_no_recommendation(self):
        """Unknown config keys don't recommend any restart."""
        result = dashboard.get_restart_recommendations(["UNKNOWN_SETTING"])
        assert result == []
