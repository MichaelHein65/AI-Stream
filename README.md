# AI Stream

Lokale Steuer-App, die per `ssh pi5` den Pi5 anweist, den Pi4-Stream auf einem der zwei gekoppelten Bluetooth-Lautsprecher abzuspielen:

- `LG Soundbar` (`LG DS77TY(1F)`)
- `Wonderboom` (`WONDERBOOM`)

Die eigentliche Wiedergabe laeuft auf dem Pi5. Deshalb kann der Mac danach geschlossen werden. Nur wenn die Bluetooth-Verbindung am Pi5 abbricht, beendet der Pi5 die Wiedergabe automatisch.

Die aktuelle Implementierung nutzt auf dem Pi5 `BlueALSA` plus `ffmpeg` und verbindet das A2DP-Profil direkt. Damit ist sie unabhaengig von PipeWire/PulseAudio-Bluetooth-Sinks.

## Start

Per Doppelklick:

- `Start AI-Stream.command`

Oder im Terminal:

```bash
cd "/Users/michaelhein/* VSC/20260320 AI-Stream"
./start_ai_stream.sh
```

Danach die App im Browser unter `http://127.0.0.1:8091` benutzen.

## Nutzung

1. Pi4-Stream-URL eintragen.
2. `URL speichern` klicken.
3. Gewuenschten Lautsprecher starten.

Beim Start kopiert die App automatisch den Controller auf den Pi5 nach `/home/pi/.ai-stream/pi5_stream_agent.py` und startet ihn dort mit `nohup`.

## Verhalten auf dem Pi5

- stoppt konkurrierende user-level PipeWire/WirePlumber-Bluetooth-Endpunkte
- verbindet den ausgewaehlten Bluetooth-Lautsprecher
- verbindet explizit das A2DP-Audioprofil
- wartet auf das BlueALSA-PCM des Geraets
- startet `ffmpeg` mit dem hinterlegten Stream direkt auf dieses Bluetooth-PCM
- beendet sich bei manuellem Stop oder Bluetooth-Abbruch

## Voraussetzungen

- `ssh pi5` funktioniert ohne Passwortabfrage
- auf dem Pi5 sind vorhanden: `python3`, `bluetoothctl`, `ffmpeg`, `busctl`, `bluealsa-cli`
- beide Lautsprecher sind bereits mit dem Pi5 gepairt

## Bereits am Pi5 angepasst

- `loginctl enable-linger pi` ist aktiv, damit user-Prozesse nach dem Schliessen des Mac weiterlaufen koennen
- `bluez-alsa-utils` und `libasound2-plugin-bluez` sind installiert

## Hinweis zur Stream-URL

Die App kennt die konkrete Pi4-Stream-URL nicht automatisch. Wenn sie noch nicht feststeht, bitte die URL des laufenden Pi4-Streams manuell eintragen.
