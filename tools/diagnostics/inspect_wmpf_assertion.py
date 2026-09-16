"""Read-only inspection of the faulting function; no hooks or memory writes."""
import sys
import struct
import pefile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wechat_shop_scraper.desktop_keyword import _visible_weixin_identity, _wmpf_root_pid, loaded_module_path
import frida

sys.stdout.reconfigure(encoding="utf-8")
pid = _wmpf_root_pid(_visible_weixin_identity()[0])
pe = pefile.PE(str(loaded_module_path(pid, "flue.dll")), fast_load=True)
unwind = next(s.get_data() for s in pe.sections if s.Name.startswith(b".pdata"))
start, end, _ = next(row for row in struct.iter_unpack("<III", unwind[:len(unwind)//12*12])
                     if row[0] <= 0x19b887b < row[1])
print("FUNCTION", hex(start), hex(end), flush=True)
session = frida.attach(pid)
script = session.create_script(r"""
const m=Process.getModuleByName('flue.dll');
for(let p=m.base.add(__START__);p.compare(m.base.add(__END__))<0;) {
 const i=Instruction.parse(p); let note='';
 try { for(const op of i.operands) if(op.type==='mem'&&op.value.base==='rip') {
  const a=i.next.add(op.value.disp); const t=a.readUtf8String(120);
  if(t&&/^[\x20-\x7e]{3}/.test(t))note=' '+t.split('\0')[0];
 }}catch(e){}
 if(p.compare(m.base.add(0x19b887b-300))>=0)send(p.sub(m.base).toString()+': '+i.toString()+note);p=i.next;
}
""".replace("__START__", str(start)).replace("__END__", str(end)))
script.on("message", lambda m, _: print(m.get("payload", m), flush=True))
try:
    script.load()
finally:
    session.detach()
