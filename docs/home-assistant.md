# Home Assistant

Prudify ships a Home Assistant integration in this repository's
`custom_components/prudify`. It shows what Prudify is doing as sensors, lets
you pause the queue and start a scan, and fires events you can build
notifications on.

## Install

1. In HACS, open **⋮ → Custom repositories**, add
   `https://github.com/Camwise1/prudify` with type **Integration**, then find
   **Prudify** in HACS and download it.
2. Restart Home Assistant.
3. **Settings → Devices & services → Add integration → Prudify.** Enter the
   address Prudify's web UI is on (for example `http://192.168.2.158:8317`)
   and its API key, from **Settings → Security** in Prudify.

If you later regenerate Prudify's API key, Home Assistant asks for the new one.

Without HACS, copy `custom_components/prudify` into your Home Assistant
`config/custom_components/` folder and restart.

## Entities

All belong to one *Prudify* device and are polled every 30 seconds.

| Entity | What it shows |
|---|---|
| `sensor.prudify_status` | `processing`, `idle`, `paused`, `suspended` (paused itself: a library is unreachable) or `holding` (outside processing hours, or Plex is playing). The reasons are attributes. |
| `sensor.prudify_current_book` | The book being worked on; author, stage and current step as attributes |
| `sensor.prudify_progress` | Percent through the current book |
| `sensor.prudify_current_book_finishes` | When the current book should be done |
| `sensor.prudify_speed` | Times faster than real time |
| `sensor.prudify_books_waiting` | Books in the queue; hours of audio as an attribute |
| `sensor.prudify_queue_finishes` | When the queue should be empty — only once every book has an estimate |
| `sensor.prudify_books_cleaned`, `_books_failed`, `_books_not_yet_cleaned` | Library totals |
| `sensor.prudify_instances_silenced`, `sensor.prudify_audio_muted` | Running totals |
| `sensor.prudify_last_finished` | The most recently finished book; status, matches and muted time as attributes |
| `binary_sensor.prudify_library_unreachable` | On while Prudify has paused itself because a library cannot be reached |
| `switch.prudify_queue` | On while the queue may run; turn off to pause it |
| `button.prudify_scan_library` | Look for new books now |

## Events

| Event | When | Data |
|---|---|---|
| `prudify_job_finished` | A book finished (or failed) | `title`, `author`, `status`, `matches`, `muted_seconds`, `mode` (`clean` or `copy`), `finished_at` |
| `prudify_queue_suspended` | The queue paused itself because a library is unreachable | `reason` |
| `prudify_queue_resumed` | …and picked itself up again | — |
| `prudify_queue_held` | Transcription started or stopped waiting for its hours or for Plex | `reason` (empty when it resumes) |

## Notification examples

Replace `notify.mobile_app_your_phone` with your own notifier.

```yaml
automation:
  - alias: Prudify finished a book
    triggers:
      - trigger: event
        event_type: prudify_job_finished
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: "Prudify: {{ trigger.event.data.title }}"
          message: >-
            {% if trigger.event.data.status == 'completed' %}
            {{ trigger.event.data.matches }} words silenced
            {%- if trigger.event.data.mode == 'copy' %} (copied unchanged){% endif %}.
            {% else %}It failed — open Prudify to see why.{% endif %}

  - alias: Prudify cannot reach the NAS
    triggers:
      - trigger: event
        event_type: prudify_queue_suspended
    actions:
      - action: notify.mobile_app_your_phone
        data:
          title: Prudify paused
          message: "{{ trigger.event.data.reason }}"
```
