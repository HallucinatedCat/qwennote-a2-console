"""Note exports matching the official app; signed URLs and downloads remain on the host."""
import ipaddress
import socket
from urllib.parse import urlsplit,parse_qsl
from aiohttp import ClientSession,ClientTimeout,DummyCookieJar,TCPConnector
from aiohttp.resolver import DefaultResolver
from note_favorites import valid_uuid
from note_translate import valid_language

URI='/r/Adaptor/MinutesExportAppI/minutesExportLocal'
HOST='shanji.dingtalk.com'
KINDS={'transcript':'content','summary':'summary'}
FORMATS={
    'txt':('TXT','txt','text/plain',True),
    'markdown':('Markdown','md','text/markdown',True),
    'srt':('SRT','srt','text/plain',True),
    'docx':('DOCX','docx','application/vnd.openxmlformats-officedocument.wordprocessingml.document',False),
    'pdf':('PDF','pdf','application/pdf',False),
}
MAX_BYTES=8*1024*1024


def format_spec(kind,file_format):
    if not isinstance(kind,str) or kind not in KINDS or not isinstance(file_format,str):raise ValueError('Invalid export format')
    spec=FORMATS.get(file_format)
    if spec is None or file_format=='srt' and kind!='transcript':raise ValueError('Invalid export format')
    return spec


def request(uuid,kind,target=None,bilingual=False,file_format='txt'):
    spec=format_spec(kind,file_format)
    if (not valid_uuid(uuid) or type(bilingual) is not bool or
        (target is None and bilingual) or (target is not None and not valid_language(target))):raise ValueError('Invalid export request')
    return [{'taskUuid':uuid,'speaker':True,'timestamp':True,'exportFileType':spec[0],
             'targetLang':target,'supportBilingual':bilingual,'exportContentType':KINDS[kind]}]


def validate_url(url,kind):
    if not isinstance(kind,str) or kind not in KINDS or not isinstance(url,str) or not 1<=len(url)<=16384 or any(ord(c)<32 or ord(c)==127 for c in url):
        raise ValueError('Invalid export URL')
    parsed=urlsplit(url)
    if (parsed.scheme!='https' or parsed.hostname!=HOST or parsed.port not in (None,443) or
        parsed.username is not None or parsed.password is not None or parsed.fragment or
        parsed.path!='/api/minutes/download/export/'+KINDS[kind]):
        raise ValueError('Unverified export origin or path')
    query=parse_qsl(parsed.query,keep_blank_values=True,strict_parsing=True)
    if len(query)!=1 or query[0][0]!='downloadToken' or not query[0][1] or any(ord(c)<32 for c in query[0][1]):
        raise ValueError('Invalid export token shape')
    return url


def receipt(reply,kind):
    body=reply.get('body') if isinstance(reply,dict) else None
    if not isinstance(reply,dict) or reply.get('code')!=200 or not isinstance(body,dict):
        raise ValueError('Export request rejected')
    if 'code' in body and (type(body['code']) is not int or body['code'] not in (0,200)):
        raise ValueError('Export request rejected')
    return validate_url(body.get('exportUrl'),kind)


class PublicResolver(DefaultResolver):
    async def resolve(self,host,port=0,family=socket.AF_INET):
        if host!=HOST or port!=443:raise ValueError('Unverified export destination')
        addresses=await super().resolve(host,port,family)
        ips=[ipaddress.ip_address(row['host']) for row in addresses]
        if not ips or any(not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip,'is_site_local',False) for ip in ips):
            raise ValueError('Non-public export destination')
        return addresses


async def download(url,kind,user_agent,file_format='txt'):
    validate_url(url,kind)
    _,_,mime,is_text=format_spec(kind,file_format)
    # Markdown MIME varies by export kind; the host always serves text/markdown.
    transport_mimes={'text/plain','text/markdown'} if file_format=='markdown' else {mime}
    resolver=PublicResolver()
    try:
        connector=TCPConnector(resolver=resolver)
        async with ClientSession(connector=connector,timeout=ClientTimeout(total=30,connect=8,sock_read=10),
                                 cookie_jar=DummyCookieJar(),trust_env=False,auto_decompress=False) as session:
            async with session.get(url,allow_redirects=False,headers={'User-Agent':user_agent,'Accept':mime,'Accept-Encoding':'identity'}) as response:
                # Binary exports may omit disposition; the host sets its own attachment header.
                disposition=response.headers.get('Content-Disposition','').split(';',1)[0].strip().lower()
                if (response.status!=200 or response.content_type not in transport_mimes or
                    response.headers.get('Content-Encoding','identity').lower()!='identity' or
                    (disposition!='attachment' and (is_text or disposition))):
                    raise ValueError('Export download has an unexpected response type')
                if response.content_length is not None and response.content_length>MAX_BYTES:raise ValueError('Export is too large')
                data=bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if len(data)+len(chunk)>MAX_BYTES:raise ValueError('Export is too large')
                    data.extend(chunk)
                if not data:raise ValueError('Export is empty')
                if file_format=='docx' and not data.startswith(b'PK\x03\x04'):raise ValueError('Invalid DOCX signature')
                if file_format=='pdf' and not data.startswith(b'%PDF-'):raise ValueError('Invalid PDF signature')
                if is_text:data.decode('utf-8-sig')
                return bytes(data)
    finally:
        await resolver.close()
