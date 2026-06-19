# Astro Malaysia EPG Grabber 📺

`content.astro.com.my` இல் இருந்து EPG data எடுத்து **XMLTV format**-ல் தினமும் auto-generate செய்யும் GitHub Actions script.

---

## 📥 EPG XML நேரடியாக பயன்படுத்த

உங்கள் IPTV player-ல் இந்த URL-ஐ EPG source ஆக add செய்யுங்கள்:

```
https://raw.githubusercontent.com/YOUR_USERNAME/astro-epg-grabber/main/astro_epg.xml
```

> `YOUR_USERNAME` → உங்கள் GitHub username மாற்றுங்கள்.

---

## 🛠 Setup

### 1. இந்த Repo-ஐ Fork செய்யுங்கள்

GitHub-ல் **Fork** button click செய்யுங்கள்.

### 2. GitHub Actions Permissions இயக்குங்கள்

```
Settings → Actions → General → Workflow permissions → Read and write permissions ✓
```

### 3. Channel IDs customize செய்யுங்கள் (optional)

`config/channels.json` file-ல் வேண்டிய channel IDs மட்டும் வையுங்கள்.

எல்லா channels-ம் வேண்டுமென்றால் `"channels": []` என்று வையுங்கள்.

### 4. Manual Run

```
Actions → Astro EPG Daily Grab → Run workflow
```

---

## 💻 Local-ல் Run செய்ய

```bash
# Install
pip install -r requirements.txt

# எல்லா channels-ம் — 3 நாள் EPG
python grab_epg.py --days 3 --output astro_epg.xml

# Specific channels மட்டும்
python grab_epg.py --channels 121 151 301 --days 2

# Config file மூலம்
python grab_epg.py --config config/channels.json --days 3

# Available channels பார்க்க
python grab_epg.py --list-channels
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output` | `astro_epg.xml` | Output file path |
| `--days` | `2` | EPG fetch days |
| `--workers` | `5` | Parallel threads |
| `--channels` | all | Specific channel IDs |
| `--config` | — | JSON config file |
| `--list-channels` | — | Print channels & exit |

---

## 📋 Output Format (XMLTV)

```xml
<?xml version='1.0' encoding='utf-8'?>
<tv generator-info-name="astro-epg-grabber" source-info-name="Astro Malaysia">
  <channel id="121">
    <display-name lang="en">Astro Vaanavil HD</display-name>
    <display-name>201</display-name>
    <icon src="https://...logo.png"/>
  </channel>

  <programme start="20260619083000 +0800" stop="20260619093000 +0800" channel="121">
    <title lang="en">Super Singer</title>
    <desc lang="en">Tamil singing competition...</desc>
    <category lang="en">Entertainment</category>
    <icon src="https://...thumbnail.jpg"/>
  </programme>
</tv>
```

---

## 🔄 Auto-Update Schedule

GitHub Actions தினமும் **12:35 AM MYT** (4:35 PM UTC) இல் run ஆகும்.

---

## 🙏 Credits

- EPG data source: [Astro Malaysia](https://content.astro.com.my)
- API: `ams-api.astro.com.my/ams/v3`
- Inspired by [azimabid00/epg](https://github.com/azimabid00/epg) & [akmalharith/epg-grabber](https://github.com/akmalharith/epg-grabber)

---

## ⚠️ Disclaimer

இந்த tool educational purpose மட்டுமே. EPG data Astro Malaysia-வுக்கு சொந்தமானது.
