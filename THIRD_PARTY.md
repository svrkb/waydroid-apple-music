# Third-party software

This repository does not contain Apple Music, Google Play, Android SDK Platform
Tools, or compiled Waydroid audio files.

The optional audio patch reads three files from the user's local Waydroid vendor
image, checks their exact hashes, and writes modified copies into that user's
Waydroid overlay directory.

The upstream Waydroid hardware code is published by the Waydroid project:

- https://github.com/waydroid/android_hardware_waydroid

Waydroid's audio policy file includes the Android Open Source Project's Apache
License 2.0 notice. Relevant upstream licensing remains with those projects.

Metadata and cover lookup uses these public services:

- MusicBrainz: https://musicbrainz.org/doc/MusicBrainz_API
- Cover Art Archive: https://musicbrainz.org/doc/Cover_Art_Archive

MusicBrainz provides its free web service for non-commercial use. Commercial
users should disable catalog artwork or arrange suitable service with
MetaBrainz. See https://musicbrainz.org/doc/MusicBrainz_API/FAQ.
