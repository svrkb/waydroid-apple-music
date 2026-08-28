# Apple Music on Waydroid, set up for Linux

This makes the Android Apple Music app feel more at home on a Linux desktop.

It can add:

- song information and controls in GNOME's media menu
- album artwork from MusicBrainz and the Cover Art Archive
- a shared 32-bit/192 kHz path from Waydroid into PipeWire
- automatic following of Linux's current output device
- protection against Bluetooth dropping into phone-call mode
- an optional 15/15 Android music volume
- an optional audio watchdog if Waydroid loses its output stream
- a tested workaround for Apple Music's rooted-device warning

Every system change is optional. For config files and host settings, the
installer records the old values so the uninstaller can put them back.

## Before you install it

Install [Waydroid](https://docs.waydro.id/usage/install-on-desktops) with a
Google Apps image. Install Apple Music from Google Play and sign in. Try playing
a song. If Apple's rooted-device warning blocks you, the installer can apply the
workaround described below.

This project does not include Apple Music, Google Play, Android platform tools,
or patched Waydroid binaries. You still need your own Apple Music subscription.
It does not bypass Apple Music's account checks, subscription checks, or DRM.

## Install

```bash
git clone https://github.com/willbeez/waydroid-apple-music.git
cd waydroid-apple-music
./install.sh
```

The installer asks a few plain yes-or-no questions. It uses `sudo` to install
missing Ubuntu packages, stop or refresh Waydroid when needed, and change the
Waydroid config or audio overlay. The Linux media bridge itself runs as your
normal user.

Run it as your normal desktop user, not with `sudo`. If you choose the audio
overlay, it stops Waydroid before changing the files. Open Apple Music again
when installation is done.

Start Apple Music when the installer is done. Android will ask whether to allow
USB debugging. Choose **Always allow** and press **Allow**. This is a local ADB
connection from your Linux account to the Waydroid container.

To turn on every feature:

```bash
./install.sh --all
```

The 192 kHz patch is deliberately strict. If your Waydroid audio files do not
match the tested checksums, it stops without installing the overlay.

## What the audio changes mean

Apple says its Android app can play ALAC from 16-bit/44.1 kHz through
24-bit/192 kHz. Apple also says hardware above 48 kHz may need an external DAC.
See [Apple's lossless audio guide](https://support.apple.com/en-us/118295).

Stock Waydroid opens its Linux audio link at 16-bit/48 kHz. The high-rate option
changes that link to 32-bit/192 kHz and keeps the shared PipeWire graph at
192 kHz. Other Linux apps still work at the same time. PipeWire resamples them
into the shared graph.

This is not automatic sample-rate switching and it does not make a 44.1 kHz
song into real 192 kHz audio. It removes the fixed 48 kHz Waydroid link. Your
DAC, Bluetooth codec, and physical output still decide what reaches your ears.
Bluetooth LDAC is good Bluetooth audio, but it is still lossy.

The binary patch currently supports one tested x86_64 Waydroid vendor build.
It checks the full input and output hashes. No Waydroid binary is stored in this
repo; the patch is made from the copy already installed on your computer.

## Linux media controls and artwork

The bridge reads Apple Music's Android media session once per second and exposes
it through Linux's normal MPRIS interface. Play, pause, previous, and next work
from GNOME's media menu.

Waydroid does not expose Apple Music's cover bitmap in a useful form. For cover
art, the bridge sends the song title, artist, and album to
[MusicBrainz](https://musicbrainz.org/) and downloads a matching front cover
from the [Cover Art Archive](https://musicbrainz.org/doc/Cover_Art_Archive).
It stores the matching image in:

```text
~/.cache/waydroid-apple-music/artwork
```

It does not send your Apple ID, password, cookies, or tokens. The local ADB
connection does have broad access to your Waydroid container, and the bridge
uses Android's unredacted notification dump to read the song title and artist.
Do not approve the ADB prompt on a host account you do not trust.

Artwork is off unless you choose it during installation. MusicBrainz's free
API is for non-commercial use. Commercial users should leave artwork off or
arrange service with MetaBrainz.

The artwork directory is private to your Linux account. The bridge keeps at
most 200 covers and 100 MB. Cover art can still be copyrighted; the Cover Art
Archive says to use images at your own risk.

You can turn artwork off in:

```text
~/.config/waydroid-apple-music/bridge.env
```

Set this value to `0`, then restart the bridge:

```text
WAYDROID_APPLE_MPRIS_CATALOG_ARTWORK=0
```

```bash
systemctl --user restart waydroid-apple-mpris.service
```

## Output switching and Bluetooth

The output-following option uses WirePlumber's normal event-driven routing.
When Linux's default output changes, the existing Waydroid stream moves with
it. There is no output-device polling timer. The two WirePlumber settings are
global, so other movable Linux audio streams follow the default output too.

The Bluetooth option disables WirePlumber's automatic headset-profile switch.
That stops Waydroid's microphone stream from pushing headphones into HFP/HSP
phone-call audio. This also applies to the whole Linux session. You will need to
choose a headset profile yourself when you actually want to use the Bluetooth
microphone.

The optional audio watchdog is separate. It watches PipeWire events. If Apple
Music says it is playing but the Waydroid output stream is missing, it waits a
one-second grace period, restarts the Waydroid session, reopens Apple Music, and
presses play. It then waits ten minutes before another recovery attempt.

The 15/15 option disables Android's safe-volume cap inside Waydroid. It can be
very loud. Turn Linux's output volume down before enabling it and use Linux's
normal volume control for listening.

## Rooted-device warning

The workaround sets Waydroid's build tags to `release-keys` and applies the
SELinux build-property value that worked on the tested image. It is not a
general root-hiding tool. Apple or Waydroid can change their checks at any time,
and you are responsible for deciding whether to use it with your account.

## Check the setup

```bash
./verify.sh
```

You can also check the bridge directly:

```bash
systemctl --user status waydroid-apple-mpris.service
```

## Waydroid updates

Uninstall the high-rate patch before updating the Waydroid image. Run the
installer again after the update. If the new image has unknown audio hashes,
the installer will refuse the patch until that build has been checked.

The normal media-control bridge does not patch Waydroid and is less sensitive
to image updates.

## Undo it

```bash
./uninstall.sh
```

This restores the Waydroid config entries, overlay files, PipeWire drop-in,
WirePlumber values, and user service that existed before installation. If you
changed one of the tracked config values afterward, the uninstaller leaves your
new value alone.

It does not uninstall Waydroid, Apple Music, or shared Ubuntu packages such as
`adb`. If you used the 15/15 option, Android's media volume and safe-volume
setting stay at their last values because the installer could not know what
they were before ADB was approved. Lower the volume in Android or with Linux's
normal volume control if needed.

## Tested on

- Ubuntu 26.04 with GNOME
- Waydroid 1.6.2
- Android 13 x86_64 MAINLINE GAPPS image
- PipeWire 1.6.2 and WirePlumber 0.5.13
- Apple Music for Android from Google Play

The MPRIS bridge should be less picky than the audio patch, but this is still a
small project tested on one machine. Keep the uninstaller nearby.

## License

The original code is under the MIT License. The installer transforms audio
files from the user's own Waydroid installation; it does not redistribute them.
See [THIRD_PARTY.md](THIRD_PARTY.md) for the upstream projects. Apple Music and Apple are
trademarks of Apple Inc. This project is not made by or endorsed by Apple,
Google, Waydroid, Canonical, PipeWire, or WirePlumber.
