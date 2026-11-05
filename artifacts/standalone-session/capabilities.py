"""Evidence levels for what the UI may claim. Stdlib only; no IO.

VERIFICATION says how well each working capability is evidenced; the UI does not show it.
Change a level only with evidence. "verified" means it was checked end to end.
"owner-check" means the host side is complete and the remaining proof is a check the user
makes on the A2 or with real notes. Functions that are blocked or out of scope are not
shown in the UI at all.
"""

# (key, label, level, detail). level: verified | owner-check
VERIFICATION=(
    ('native_reads','Native reads','verified','verified'),
    ('quotas','Membership and quotas','verified','current application account/device quotas and 4G usage verified; unreported text-summary and billing-cycle fields remain explicit'),
    ('native_renewal','Native token renewal','verified','proactive renewal verified; natural-expiry renewal verified on the isolated validation lineage'),
    ('recording','Recording controls','verified','start, pause, resume and stop confirmed on the A2; each command is followed by a device check'),
    ('record_task','Device check','verified','confirmed: follows a stop, a button-started recording and a switch-off (the console showed not recording about 22 s after power-off; offline appears only once the cloud reports it)'),
    ('note_favorite','Note favorites','verified','application-path favorite and removal verified on a disposable note; original state restored'),
    ('device_identity','Device identity','verified','name, serial, firmware and battery read from the real bound-device response (firmware and battery from the device properties)'),
    ('device_name','Device name editing','verified','rename and restore confirmed'),
    ('note_sharing','Note sharing','verified','private and public switching and the share password confirmed'),
    ('native_transcript','Saved transcript','verified','text, speakers and times verified; More reaches the end of a long note'),
    ('note_replace','Transcript replacement','verified','literal replacement and exact restoration verified on a disposable note; marked paragraphs, large batches and server-side locking are out of scope'),
    ('note_tags','Note and account tags','verified','creation, rename, automatic matching, deletion and membership replacement verified with restoration; an incomplete tag list is refused'),
    ('note_summary','Summary editing','verified','default Markdown summary edit and exact restoration verified; other templates and rich summaries are out of scope'),
    ('note_regenerate','Summary regeneration','verified','default two-stage regeneration, completion and exact restoration verified; other templates are out of scope'),
    ('note_translate','Note translation','verified','title, summary, key points, action items and transcript confirmed'),
    ('note_export','Note export','verified','application-path original/translated/bilingual transcript and default-summary exports in TXT, Markdown, DOCX and PDF, plus transcript SRT; all format combinations and bilingual summary verified'),
    ('note_media','Original media saving','verified','original audio of a recording saved from Recordings: WAV only (the A2 uploads WAV; no ogg or mp3), and an incognito recording must be saved before transcription finishes, or with automatic transcription off'),
    ('settings_write','Settings writes','verified','effects confirmed; settings can change while recording'),
    ('live_transcript','Live transcript','verified','recording with the Live transcript switch uses a realtime transcription session, as the app does for a 4G A2; live rows confirmed'),
    ('rtc_playback','Realtime session','verified','joining, keeping and leaving the session and receiving nonzero audio verified; playback and live translation are not built'),
    ('recordings','Recordings','owner-check','list, original audio, make a note, recycle bin and restore built from the DeviceFileI contract; original audio confirmed (WAV, before transcription finishes); make a note, recycle bin and restore remain'),
    ('record_save_audio','Download original audio after Stop','verified','with the Recorder switch on, Stop downloaded the stopped recording as WAV (journal phase complete); the device-check fid matches the recordings list fid. The page asks recording-audio-latest every 5 s, then 15 s, for up to 15 minutes'),
    ('dangerous_actions','Dangerous actions','verified','note delete, recording delete and restore, and voiceprint clear confirmed behind a switch and a typed word; unbind is offered but deliberately untested'),
    ('mcp_writes','MCP write path','verified','title, summary, find and replace, speaker and hot words on a separate write grant; note rename and summary save confirmed'),
    ('account_settings','Account settings','owner-check','smart slicing and voiceprint on/off from the account-settings contract; change one and reload'),
    ('voice_button','Voice button','owner-check','cloud setting written as the app writes it; the app also sends a Bluetooth-only device command, so check that a press does the chosen action'),
    ('builtin_assistant','Built-in AI Assistant','verified','general and note-scoped Q&A, continued sessions, current history, explicit task-result recovery and vendor-wide listing/post-restart restoration are runtime-verified'),
)
LEVELS=('verified','owner-check')
VIEWS=('live','assistant','notes','memos','settings','dev')  # dev = Dev tools tab: test-only functions


def verification_levels():
    return [{'key':key,'label':label,'level':level,'detail':detail} for key,label,level,detail in VERIFICATION]


def verification_details():
    return {key:detail for key,_,_,detail in VERIFICATION}
