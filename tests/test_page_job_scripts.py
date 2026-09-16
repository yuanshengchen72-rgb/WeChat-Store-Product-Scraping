"""Execute the production page-job scripts in an isolated JS runtime."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

from wechat_shop_scraper.desktop_keyword import DesktopWechatBackend


class PageJobScriptTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the isolated JS test')
    def test_done_poll_returns_value_and_deletes_job_but_pending_poll_keeps_it(self):
        backend = DesktopWechatBackend()
        backend.evaluate = mock.Mock(side_effect=[{'jobStarted': True}, {'done': False},
                                                  {'done': True, 'value': {'ok': True}}])
        backend._evaluate_page_async('new Promise(resolve=>{globalThis.__release=resolve;})', 30)
        calls = backend.evaluate.call_args_list
        scripts = [calls[0].args[0], calls[1].args[0]]
        source = r'''
const assert=require('node:assert/strict');
const fs=require('node:fs');
const [start,poll]=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{
 assert.deepEqual(JSON.parse(eval(start)),{jobStarted:true});
 await new Promise(setImmediate);
 assert.deepEqual(JSON.parse(eval(poll)),{done:false});
 assert.equal(Object.keys(globalThis).filter(k=>k.startsWith('__collector_page_')).length,1);
 const expected={ok:true,products:[{product_id:'1001'}],hasMore:true,cursor:'next'};
 globalThis.__release(expected);
 await new Promise(setImmediate);
 assert.deepEqual(JSON.parse(eval(poll)),{done:true,value:expected});
 assert.equal(Object.keys(globalThis).filter(k=>k.startsWith('__collector_page_')).length,0);
 assert.deepEqual(JSON.parse(eval(poll)),{missing:true});
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', source], input=json.dumps(scripts),
                                text=True, capture_output=True, timeout=10,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
