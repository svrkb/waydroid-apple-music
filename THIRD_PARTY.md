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

- Deezer: https://developers.deezer.com/api

Deezer's search API is public and free to call without an API key, though it
is rate limited and offers no service level agreement. It is used only as a
fallback when the iTunes Search API has no match. Commercial users should
review Deezer's terms before enabling catalog artwork.
