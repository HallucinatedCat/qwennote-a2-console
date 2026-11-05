"""Read application media metadata and save one original audio file privately."""
import asyncio
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
from urllib.parse import urlsplit
import aiohttp
import note_summary

class MediaUnavailable(ValueError):
    def __init__(self,reason):self.reason=reason;super().__init__('Original media unavailable')

DETAIL_URI=note_summary.DETAIL_URI
PLAY_URI='/r/Adaptor/PortalMinutesI/queryPlayInfo'
DOWNLOAD_URI='/r/Adaptor/PortalMinutesI/queryDownloadUrl'
MAX_BYTES=512*1024*1024
URL_MAX=4096
HOST_SUFFIXES=('dingtalk.com','alicdn.com','aliyuncs.com')
MIME_EXTENSIONS={'audio/ogg':'.ogg','audio/opus':'.opus','application/ogg':'.ogg','audio/mpeg':'.mp3','audio/mp3':'.mp3','audio/mp4':'.m4a','audio/x-m4a':'.m4a','audio/aac':'.aac','audio/wav':'.wav','audio/x-wav':'.wav'}


def download_request(uuid):
    if not isinstance(uuid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',uuid):raise ValueError('Invalid note')
    return [{'uuid':uuid,'needSubTitle':False}]


def _body(reply,stage):
    if not isinstance(reply,dict) or reply.get('code')!=200:raise MediaUnavailable(stage+'_transport')
    body=reply.get('body')
    if body is None:raise MediaUnavailable(stage+'_empty')
    if not isinstance(body,dict):raise MediaUnavailable(stage+'_type')
    if not note_summary.acknowledged(reply):raise MediaUnavailable(stage+'_business')
    return body


def _integer(*values):
    for value in values:
        if isinstance(value,bool):continue
        try:number=int(value)
        except (TypeError,ValueError,OverflowError):continue
        if number>=0:return number
    return None


def validate_url(url):
    if not isinstance(url,str) or not 1<=len(url)<=URL_MAX or '\\' in url or any(ord(c)<33 or ord(c)==127 for c in url):raise ValueError('Invalid media URL')
    try:
        parsed=urlsplit(url);host=(parsed.hostname or '').lower();port=parsed.port
    except ValueError:raise ValueError('Invalid media URL') from None
    if parsed.scheme!='https' or not host or host.endswith('.') or parsed.username is not None or parsed.password is not None or port is not None or parsed.fragment or not parsed.path:raise ValueError('Invalid media URL')
    if not any(host==suffix or host.endswith('.'+suffix) for suffix in HOST_SUFFIXES):raise ValueError('Invalid media URL')
    return url


def select(detail_reply,play_reply,download_reply,uuid):
    detail=_body(detail_reply,'detail').get('data');play=_body(play_reply,'play')
    try:download=_body(download_reply,'download')
    except MediaUnavailable as error:
        if not error.reason.startswith('download_'):raise
        download={}
    if not isinstance(detail,dict):raise MediaUnavailable('detail_data')
    if detail.get('uuid')!=uuid:raise MediaUnavailable('detail_identity')
    if detail.get('mediaType')!='audio':raise MediaUnavailable('detail_media_type')
    if detail.get('isIncognitoMode') is True:raise MediaUnavailable('detail_incognito')
    if play.get('statusCode',200)!=200:raise MediaUnavailable('play_status')
    if download.get('isLiveTask') is True:raise MediaUnavailable('live_task')
    record=detail.get('record') if isinstance(detail.get('record'),dict) else {}
    av_list=play.get('avList') if isinstance(play.get('avList'),list) else []
    av_file=next((row.get('fileUrl') for row in av_list if isinstance(row,dict) and row.get('type')==1),None)
    candidates=[('download',download.get('exportUrl')),('mobile_play',play.get('mobileAudioUrl')),('pc_play',play.get('pcAudioUrl')),('audio_play',av_file)]
    source,url=next(((name,value) for name,value in candidates if isinstance(value,str) and value.strip()),(None,None))
    if url is None:raise MediaUnavailable('url_missing')
    return {'url':validate_url(url.strip()),'source':source,
            'expected_size':_integer(play.get('size'),detail.get('size'),record.get('size')),
            'expected_duration_ms':_integer(play.get('duration'),detail.get('duration'),record.get('duration'))}


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Pin one HTTPS origin to public DNS results; redirects remain disabled."""
    def __init__(self,host,port):self.host=host;self.port=port;self.loop=asyncio.get_running_loop()
    async def resolve(self,host,port=0,family=socket.AF_INET):
        if host!=self.host:raise ValueError('Unexpected media host')
        infos=await self.loop.getaddrinfo(host,self.port,type=socket.SOCK_STREAM,family=socket.AF_UNSPEC)
        addresses=[]
        for info in infos:
            address=ipaddress.ip_address(info[4][0])
            if not address.is_global:raise ValueError('Media origin is not public')
            addresses.append(address)
        if not addresses:raise ValueError('Media origin has no address')
        return [{'hostname':host,'host':str(address),'port':self.port,'family':socket.AF_INET6 if address.version==6 else socket.AF_INET,'proto':0,'flags':0} for address in addresses]
    async def close(self):pass


def probe(path):
    if shutil.which('ffprobe') is None:raise ValueError('Media probe unavailable')
    try:
        result=subprocess.run(['ffprobe','-v','error','-show_entries','format=format_name,duration,size:stream=codec_type,codec_name','-of','json',os.fspath(path)],capture_output=True,timeout=30,check=False)
        if result.returncode:raise ValueError
        data=json.loads(result.stdout);fmt=data['format'];audio=next(row for row in data.get('streams',[]) if row.get('codec_type')=='audio')
        if fmt.get('size') is not None and int(fmt['size'])!=path.stat().st_size:raise ValueError
        duration=float(fmt['duration']) if fmt.get('duration') not in (None,'','N/A') else None
        if duration is not None and (not math.isfinite(duration) or duration<0):raise ValueError
        name=str(fmt['format_name']);codec=str(audio.get('codec_name') or 'unknown')
        if not name or len(name)>128 or len(codec)>64:raise ValueError
        return {'format':name,'codec':codec,'duration_seconds':duration}
    except (KeyError,TypeError,ValueError,StopIteration,json.JSONDecodeError,OSError,subprocess.SubprocessError,OverflowError):raise ValueError('Invalid audio media') from None


def _extension(mime,probe_info):
    if mime in MIME_EXTENSIONS:return MIME_EXTENSIONS[mime]
    name=probe_info['format']
    if 'ogg' in name:return '.ogg'
    if 'mp3' in name:return '.mp3'
    if 'm4a' in name or 'mp4' in name or 'mov' in name:return '.m4a'
    if 'wav' in name:return '.wav'
    if 'aac' in name:return '.aac'
    return '.bin'


class DownloadFailed(ValueError):
    """stage and error_type name where a download stopped; never the URL, headers or reply."""
    def __init__(self,stage,error_type):
        self.stage=stage;self.error_type=error_type;super().__init__('Original media download failed; no file was saved')


async def download(url,user_agent,directory,expected_size=None,headers=None):
    """headers: extra request headers the cloud returned with the URL (DeviceFileI downloads)."""
    stage='url';resolver=None;temp=None;final=None;renamed=False
    try:
        validate_url(url)
        stage='setup'
        if shutil.which('ffprobe') is None:raise ValueError('Media probe unavailable')
        if expected_size is not None and expected_size>MAX_BYTES:raise ValueError('Media file too large')
        directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():raise ValueError('Invalid media directory')
        directory.chmod(0o700)
        if shutil.disk_usage(directory).free<min(expected_size or 1024*1024,MAX_BYTES):raise ValueError('Insufficient media storage')
        parsed=urlsplit(url);resolver=PublicResolver(parsed.hostname,443)
        stage='connect'
        timeout=aiohttp.ClientTimeout(total=300,connect=10,sock_read=30)
        connector=aiohttp.TCPConnector(resolver=resolver)
        async with aiohttp.ClientSession(connector=connector,headers={'User-Agent':user_agent,'Accept':'audio/*,application/octet-stream'},timeout=timeout,auto_decompress=False) as session:
            async with session.get(url,allow_redirects=False,**({'headers':headers} if headers else {})) as response:
                status=getattr(response,'status',200)
                if status!=200:stage=f'http-{status//100}xx';raise ValueError
                stage='encoding'
                if response.headers.get('Content-Encoding','identity').lower()!='identity':raise ValueError
                stage='content-type';mime=response.content_type
                if mime not in set(MIME_EXTENSIONS)|{'application/octet-stream','binary/octet-stream'}:raise ValueError
                stage='length';length=response.content_length
                if length is not None and not 0<length<=MAX_BYTES:raise ValueError
                stage='read'
                temp=directory/('.'+secrets.token_hex(16)+'.part');digest=hashlib.sha256();size=0
                descriptor=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as handle:
                    async for chunk in response.content.iter_chunked(64*1024):
                        size+=len(chunk)
                        if size>MAX_BYTES:raise ValueError
                        handle.write(chunk);digest.update(chunk)
                if not size or (length is not None and size!=length):raise ValueError
                stage='probe';probe_info=await asyncio.to_thread(probe,temp)
                stage='save';final=directory/(secrets.token_hex(16)+_extension(mime,probe_info))
                if final.exists():raise ValueError
                temp.replace(final);renamed=True;final.chmod(0o600)
                return {'path':final,'filename':final.name,'bytes':size,'sha256':digest.hexdigest(),'mime':mime,'probe':probe_info}
    except BaseException as exc:
        if temp is not None:temp.unlink(missing_ok=True)
        if renamed and final is not None:final.unlink(missing_ok=True)
        if isinstance(exc,(asyncio.CancelledError,KeyboardInterrupt,SystemExit)):raise
        raise DownloadFailed(stage,type(exc).__name__) from None
    finally:
        if resolver is not None:await resolver.close()
