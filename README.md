# dummy-edid

Add a custom resolution — any aspect ratio — to an HDMI/DP dummy plug by rewriting the
EDID stored on the plug itself. One Python file, standard library only.

## Why

Headless game-streaming hosts (Sunshine + Moonlight, Steam Remote Play) use a dummy plug
so the GPU thinks a monitor is attached. The stream can only be the size of that fake
monitor, and commodity plugs only advertise 16:9. Stream to a 3:2 Surface, a 16:10
laptop or a 21:9 ultrawide and you get black bars.

Compositor custom modes break with updates, and software EDID overrides depend on the
driver. Writing the mode into the plug survives all of that: the GPU just sees a monitor
that supports it.

Built for a Surface Studio 2+ (4500x3000, 3:2) streaming from an RTX 2060 SUPER on
Pop!_OS/COSMIC through a Xieoery X15 4K plug. That plug now advertises 3600x2400@60, and
the stream fills the screen.

## Usage

```sh
sudo modprobe i2c-dev
sudo ./dummy_edid.py list                          # outputs and their timing slots
sudo ./dummy_edid.py add HDMI-A-1 3600x2400@60     # dry run: shows the change, saves a backup
sudo ./dummy_edid.py add HDMI-A-1 3600x2400@60 --yes
# reboot (or replug the plug), then select the new mode in your display settings
```

Undo: `sudo ./dummy_edid.py restore HDMI-A-1 backups/<file>.bin --yes`

`--slot N` picks which timing to replace (default: the last one; slot 1, the preferred
mode, is never touched). Pick a slot that duplicates a standard mode, such as a 1080p60
timing.

Offline, for `drm.edid_firmware` users or to inspect first:
`./dummy_edid.py add plug.bin 3600x2400@60 --file -o plug-3x2.bin`

## Safety

- **The plug is identified by its contents, not by a guessed bus number.** Every I2C bus
  is probed read-only at 0x50, and the tool writes only to the one bus whose bytes exactly
  match the connector's EDID in sysfs. If zero or several buses match, it stops.
- **Point it only at a dummy plug.** Real monitors may also have a writable EEPROM, and
  rewriting one can break its picture.
- A backup is saved before anything is written. Only the changed bytes are written, and
  the whole chip is read back and compared.
- **Modes the link can't carry are refused.** The new timing uses CVT reduced blanking
  v2, and its pixel clock is checked against the plug's own range limit and `--max-clock`
  (default 600 MHz for HDMI 2.0; use 340 for HDMI 1.4). Example: 3840x2560@60 needs about
  619 MHz, so on HDMI 2.0 use 3600x2400@60 (545 MHz) instead.
- Some plugs are write-protected. Then the readback fails and nothing has changed.

## Tests

`python3 -m unittest discover tests` covers the following, using the real plug's factory
EDID:

- it rebuilds byte-for-byte the EDID that is running on that plug
- it rejects a mode whose pixel clock is too high
- it refuses to replace slot 1
- checksums stay valid, and only the target slot changes

## Credits

Built by Brooks with [Claude](https://claude.com/claude-code) (Anthropic) as a coding partner; most commits are co-authored by Claude.

## License

MIT, see `LICENSE`.
