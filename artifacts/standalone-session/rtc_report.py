#!/usr/bin/env python3
"""Offline, read-only RTC participant evidence. Never emit identities, tokens or SDP."""
import argparse
import json
import math
from pathlib import Path
import re
from capture_report import read_private
from live_diagnostics import protected_directory
from device_protocol import decode_push,TRANSCRIPT_STREAMS
from live_transcript import parse_transcript
import record_tasks


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def task_evidence(directory,result,start,end):
    capture=result.get('capture') or {};identifier=capture.get('capture_id')
    if identifier is None:return {'available':False,'physical_state_verified':False}
    if not isinstance(identifier,str) or not re.fullmatch(r'[0-9a-f]{24}',identifier):raise ValueError('Invalid capture handle')
    base=directory/'live-captures';path=base/identifier
    protected_directory(base);protected_directory(path)
    receipt=json.loads(read_private(directory/'create-response.json',2*1024*1024))
    device=receipt['body']['rtcTranslateTask']['deviceTask']
    config={'account_id':device['accountId'],'account_type':device['accountType'],'target_device_id':device['deviceId']}
    phases={name:{'true':0,'false':0,'unknown':0} for name in ('before_join','during_observation','cleanup','unknown_window')}
    matched=invalid=0;recordings=set();started=capture.get('started_at')
    window=finite(start) and finite(end) and end>start
    for line in read_private(path/'pushes.jsonl',8*1024*1024).splitlines():
        try:
            row=json.loads(line)
            if not isinstance(row,dict):raise ValueError('Invalid capture row')
            if row.get('kind')!='push':continue
            elapsed=row.get('elapsed_seconds')
            if not finite(started) or not finite(elapsed) or elapsed<0:raise ValueError('Invalid capture time')
            at=started+elapsed
            phase='unknown_window' if not window else 'before_join' if at<start else 'during_observation' if at<end else 'cleanup'
            for event in decode_push(row.get('packet')):
                try:task=record_tasks.from_event(config,event)
                except (ValueError,TypeError,KeyError):invalid+=1;continue
                if task is None:continue
                matched+=1;recordings.add(task.fid)
                flag='true' if task.real_time_asr is True else 'false' if task.real_time_asr is False else 'unknown'
                phases[phase][flag]+=1
        except (ValueError,TypeError,KeyError,AttributeError):invalid+=1
    return {'available':True,'matched_events':matched,'invalid_events':invalid,'distinct_recordings':len(recordings),
            **{'real_time_asr_'+flag:sum(p[flag] for p in phases.values()) for flag in ('true','false','unknown')},
            **phases,'physical_state_verified':False}


def transcript_evidence(directory,result,start,end):
    """Attribute transcript candidates to the created RTC task without emitting IDs or text."""
    capture=result.get('capture') or {};identifier=capture.get('capture_id')
    if identifier is None:return {'available':False,'physical_state_verified':False}
    if not isinstance(identifier,str) or not re.fullmatch(r'[0-9a-f]{24}',identifier):raise ValueError('Invalid capture handle')
    base=directory/'live-captures';path=base/identifier
    protected_directory(base);protected_directory(path)
    receipt=json.loads(read_private(directory/'create-response.json',2*1024*1024))
    task=receipt['body']['rtcTranslateTask'];expected=task.get('rtcTranslateTaskId')
    if type(expected) is not int or expected<=0:return {'available':False,'reason':'receipt_identity_unavailable','physical_state_verified':False}
    conf=task.get('moziConfCreateResultDTO') or {};conference_start=conf.get('startTime')
    if type(conference_start) is not int or conference_start<0:raise ValueError('Invalid RTC conference start')
    started=capture.get('started_at');window=finite(start) and finite(end) and end>start
    rows=[];invalid=0;tasks=set();records=set();sources={name:0 for name in TRANSCRIPT_STREAMS.values()}
    for line in read_private(path/'pushes.jsonl',8*1024*1024).splitlines():
        try:
            row=json.loads(line)
            if not isinstance(row,dict) or row.get('kind')!='push':continue
            elapsed=row.get('elapsed_seconds')
            if not finite(started) or not finite(elapsed) or elapsed<0:raise ValueError('Invalid capture time')
            at=started+elapsed
            phase='unknown_window' if not window else 'before_join' if at<start else 'during_observation' if at<end else 'cleanup'
            for event in decode_push(row.get('packet')):
                if event.get('type') not in TRANSCRIPT_STREAMS:continue
                parsed=parse_transcript(event.get('payload'),conference_start=conference_start)
                if parsed is None:invalid+=1;continue
                source=TRANSCRIPT_STREAMS[event['type']];sources[source]+=1
                tasks.add(parsed['task_id']);records.add(parsed['record_id'])
                rows.append((phase,at-start if window else None,parsed['task_id']==expected))
        except (ValueError,TypeError,KeyError,AttributeError):invalid+=1
    joined=[row for row in rows if row[0]=='during_observation'];matches=[row for row in rows if row[2]]
    joined_matches=[row for row in joined if row[2]]
    first=min((row[1] for row in joined_matches if finite(row[1])),default=None)
    return {'available':True,'parsed_candidates':len(rows),'invalid_candidates':invalid,
            'receipt_task_matches':len(matches),'other_task_candidates':len(rows)-len(matches),
            'distinct_task_identities':len(tasks),'distinct_record_identities':len(records),
            'joined_window_candidates':len(joined),'joined_receipt_task_matches':len(joined_matches),
            'first_receipt_match_after_join_seconds':round(first,3) if first is not None else None,
            'receipt_task_match_observed':bool(matches),
            'all_parsed_candidates_match_receipt':len(matches)==len(rows) if rows else None,
            'source_candidates':sources,'conference_start_present':conference_start>0,
            'physical_state_verified':False,'private_content_emitted':False}


def analyze(directory):
    directory=Path(directory)
    protected_directory(directory);protected_directory(directory.parent)
    def load(name):return json.loads(read_private(directory/name,2*1024*1024))
    login=load('mcs-login-ack.json')
    if not isinstance(login,list) or len(login)<2 or login[0]!='ok' or not isinstance(login[1],dict):
        raise ValueError('Invalid login receipt')
    body=login[1];room=body.get('room')
    if not isinstance(room,dict) or not isinstance(room.get('participants'),list):raise ValueError('Missing participant roster')
    own=body.get('id');peers=set()
    if not isinstance(own,str) or not own:raise ValueError('Missing receiver identity')
    for row in room['participants']:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row['id']:raise ValueError('Invalid participant roster')
        if row['id']!=own:peers.add(row['id'])
    start=load('conference-join-request.json').get('sent_at')
    end=load('device-stop-request.json').get('sent_at')
    valid_window=finite(start) and finite(end) and end>start
    result=load('result.json');mcs=result.get('mcs',{})
    complete=valid_window and mcs.get('trace_truncated') is False and mcs.get('error_type') is None
    departures=set();first=None;invalid=0;rows=0
    raw=read_private(directory/'mcs-frames.jsonl',4*1024*1024)
    for line in raw.splitlines():
        rows+=1
        try:
            row=json.loads(line)
            if not isinstance(row,dict) or not finite(row.get('time')):raise ValueError('Invalid trace row')
            frame=row.get('frame')
            if not isinstance(frame,str) or row.get('direction') not in {'in','out'}:raise ValueError('Invalid trace frame')
            if row['direction']!='in' or not frame.startswith('42'):continue
            match=re.fullmatch(r'42[0-9]*(\[.*)',frame,re.S)
            if not match:raise ValueError('Invalid event envelope')
            event=json.loads(match[1])
            if not isinstance(event,list) or not event or not isinstance(event[0],str):raise ValueError('Invalid event')
            if event[0]!='participant':continue
            if len(event)<2 or not isinstance(event[1],dict):raise ValueError('Invalid participant event')
            payload=event[1]
            if payload.get('action')!='leave':continue
            data=payload.get('data');identity=data.get('id') if isinstance(data,dict) else data
            if not isinstance(identity,str):raise ValueError('Invalid leave identity')
            if valid_window and start<=row['time']<end and identity in peers:
                departures.add(identity)
                elapsed=row['time']-start;first=elapsed if first is None else min(first,elapsed)
        except (ValueError,TypeError,RecursionError):invalid+=1
    complete=bool(complete and invalid==0)
    # No observed departure is weaker than presence, audio attribution or a physical device read.
    no_leave=False if departures else True if complete and peers else None
    return {'initial_remote_participants':len(peers),'peer_departures_during_observation':len(departures),
            'first_peer_leave_after_join_seconds':round(first,3) if first is not None else None,
            'no_peer_leave_observed':no_leave,'observation_trace_complete':complete,
            'trace_rows':rows,'invalid_rows':invalid,'physical_device_identity_verified':False,
            'private_content_emitted':False,'record_tasks':task_evidence(directory,result,start,end),
            'transcript_receipts':transcript_evidence(directory,result,start,end)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    try:result=analyze(args.directory)
    except Exception as error:
        print(json.dumps({'report_failed':True,'error_type':type(error).__name__,'private_details_omitted':True}));return 2
    print(json.dumps(result,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
