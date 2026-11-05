#!/usr/bin/env python3
"""Replay a protected push capture and print counts only, never transcript/IDs/tokens."""
import argparse
import json
import os
from pathlib import Path
import stat
from device_protocol import decode_push,TRANSCRIPT_STREAMS
from live_transcript import TranscriptStore,parse_transcript
from live_diagnostics import protected_directory


def read_private(path,limit):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_size>limit:
            raise ValueError('Invalid private capture file')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            value=stream.read(limit+1)
            if len(value)>limit:raise ValueError('Capture file exceeds limit')
            return value
    finally:os.close(fd)


def analyze(directory):
    directory=Path(directory)
    protected_directory(directory);protected_directory(directory.parent)
    summary=json.loads(read_private(directory/'summary.json',64*1024))
    if not isinstance(summary,dict):raise ValueError('Invalid capture summary')
    raw=read_private(directory/'pushes.jsonl',8*1024*1024)
    result={'capture_was_finalized':summary.get('active') is False,
            'raw_lines':0,'invalid_lines':0,'push_frames':0,'connection_markers':0,
            'decoder_errors':0,'unrecognized_frames':0,'device_events':0,'transcript_candidates':0,
            'valid_transcripts':0,'invalid_transcripts':0,'sequence_decreases':0,
            'dinger_transcript_candidates':0,'conference_transcript_candidates':0,
            'replayed_updates':0,'final_paragraphs_in_bounded_replay':0,
            'hardware_behavior_verified':False,'private_content_emitted':False}
    store=TranscriptStore();sequences={}
    for line in raw.splitlines():
        result['raw_lines']+=1
        try:row=json.loads(line)
        except (ValueError,RecursionError):result['invalid_lines']+=1;continue
        if not isinstance(row,dict):result['invalid_lines']+=1;continue
        if row.get('kind')=='connection':result['connection_markers']+=1;store.mark_reconnect();continue
        if row.get('kind')!='push':result['invalid_lines']+=1;continue
        result['push_frames']+=1
        try:events=decode_push(row.get('packet'))
        except (ValueError,TypeError,KeyError,AttributeError):result['decoder_errors']+=1;continue
        if not events:result['unrecognized_frames']+=1
        for event in events:
            if event['type']==770000:result['device_events']+=1;continue
            result['transcript_candidates']+=1
            source=TRANSCRIPT_STREAMS[event['type']]
            result['dinger_transcript_candidates' if event['type']==770001 else 'conference_transcript_candidates']+=1
            parsed=parse_transcript(event['payload'])
            if parsed is None:result['invalid_transcripts']+=1;continue
            result['valid_transcripts']+=1
            key=(source,parsed['task_id'],parsed['record_id'],parsed['language'],parsed['translated'])
            sequence=parsed['sequence']
            if sequence>0:
                if key in sequences and sequence<sequences[key]:result['sequence_decreases']+=1
                sequences[key]=max(sequence,sequences.get(key,0))
            result['replayed_updates']+=int(store.accept(event['payload'],source=source) is not None)
    result['final_paragraphs_in_bounded_replay']=sum(row['final'] for row in store.snapshot()['rows'])
    result['replay_evicted']=store.evicted
    # All values copied to stdout are bounded types, never arbitrary stored strings.
    for name in ('queue_dropped','connections','disconnects'):
        value=summary.get(name)
        result['recorded_'+name]=value if type(value) is int and 0<=value<2**63 else None
    result['capture_truncated']=summary.get('finish_reason') in {'byte_limit','row_limit','io_error'}
    result['possible_missing_events']=bool(result['recorded_queue_dropped'] or result['recorded_disconnects'] or
                                          not result['capture_was_finalized'] or result['invalid_lines'] or result['capture_truncated'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    try:
        result=analyze(args.directory)
        print(json.dumps(result,indent=2));return 0
    except Exception:
        print('Capture analysis failed; private paths, values and exception details omitted.');return 2

if __name__=='__main__':raise SystemExit(main())
