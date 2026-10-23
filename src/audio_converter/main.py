"""Main entry point for the audio converter application."""

import sys
from pathlib import Path

# Ensure src is in path when running directly
src_path = Path(__file__).parent.parent
if str(src_path) not in sys.path:
    sys.path.insert(0, str(src_path))

from audio_converter.gui import main  # noqa: E402

if __name__ == "__main__":
    main()