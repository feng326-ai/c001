"""生成 v2 agent 的 HMAC 命令信封；运行时密钥不入库。"""
import argparse, base64, hashlib, hmac, json
from pathlib import Path

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--key-file', required=True)
    p.add_argument('--payload-file', required=True)
    p.add_argument('--output', required=True)
    a=p.parse_args()
    key=base64.b64decode(Path(a.key_file).read_text(encoding='ascii').strip(), validate=True)
    payload=Path(a.payload_file).read_bytes()
    json.loads(payload)
    sig=hmac.new(key,payload,hashlib.sha256).digest()
    Path(a.output).write_text(json.dumps({'payload_b64':base64.b64encode(payload).decode(),'signature_b64':base64.b64encode(sig).decode()},separators=(',',':')),encoding='utf-8')
if __name__=='__main__': main()
