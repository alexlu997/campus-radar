"""Bounded request recovery, DNS renewal and TLS boundaries without system changes."""

from email.message import Message
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from campus_radar.collector import CollectionError, Fetcher
from campus_radar.network import DirectTransport, TransportError, physical_interface


HOST = "news.sjtu.edu.cn"
URL = "https://" + HOST + "/xsjz/index.html"


class NetworkTests(unittest.TestCase):
    def test_interface_is_read_each_time_and_never_assumes_en0(self):
        values = ["PrimaryInterface : en0", "PrimaryInterface : en7", "PrimaryInterface : utun9"]
        with patch("campus_radar.network.subprocess.run", side_effect=[subprocess.CompletedProcess([],0,v) for v in values]) as run:
            self.assertEqual(physical_interface(), "en0")
            self.assertEqual(physical_interface(), "en7")
            with self.assertRaises(TransportError):
                physical_interface()
            self.assertTrue(all(c.kwargs["input"].startswith("show ") for c in run.call_args_list))

    def test_dns_ttl_and_interface_change_trigger_fresh_resolution(self):
        transport = DirectTransport(5,1024,"test")
        answer = json.dumps({"Status":0,"Answer":[{"type":1,"TTL":10,"data":"202.120.2.114"}]}).encode()
        with patch.object(transport,"_curl",return_value=(answer,200,Message())) as query, patch("campus_radar.network.time.monotonic",return_value=100) as clock:
            transport.resolve(HOST,"en0")
            transport.resolve(HOST,"en0")
            self.assertEqual(query.call_count,1)
            clock.return_value=111
            transport.resolve(HOST,"en0")
            transport.resolve(HOST,"en7")
            self.assertEqual(query.call_count,3)

    def test_fake_ip_or_private_dns_answer_is_not_used(self):
        transport = DirectTransport(5,1024,"test")
        for address in ["198.18.0.1","127.0.0.1","10.1.2.3"]:
            answer=json.dumps({"Status":0,"Answer":[{"type":1,"TTL":30,"data":address}]}).encode()
            with patch.object(transport,"_curl",return_value=(answer,200,Message())):
                with self.assertRaisesRegex(TransportError,"non-public"):
                    transport.resolve(HOST,"en0")

    def test_redirect_is_checked_before_next_request(self):
        transport=DirectTransport(5,1024,"test")
        headers=Message();headers['Location']='https://login.example/secret'
        with patch("campus_radar.network.physical_interface",return_value="en0"), patch.object(transport,"resolve",return_value=["202.120.2.114"]), patch.object(transport,"_curl",return_value=(b"",302,headers)) as request:
            with self.assertRaisesRegex(TransportError,"verified HTTPS"):
                transport.get(URL,[HOST])
            self.assertEqual(request.call_count,1)

    def test_switch_during_request_rechecks_interface_and_dns(self):
        with tempfile.TemporaryDirectory() as folder:
            fetcher=Fetcher(folder,direct_hosts=[HOST],delay=0,retries=1)
            headers=Message();headers['Content-Type']='text/html'
            with patch("campus_radar.network.physical_interface",side_effect=["en0","en7"]) as interface, patch.object(fetcher.direct,"resolve",return_value=["202.120.2.114"]) as resolve, patch.object(fetcher.direct,"_curl",side_effect=[TransportError("connection reset"),(b"complete page",200,headers)]):
                raw,_,_=fetcher._request(URL,[HOST])
                self.assertEqual(raw,b"complete page")
                self.assertEqual(interface.call_count,2)
                self.assertEqual([c.args[1] for c in resolve.call_args_list],["en0","en7"])

    def test_tls_failure_is_not_retried_or_relaxed(self):
        with tempfile.TemporaryDirectory() as folder:
            fetcher=Fetcher(folder,direct_hosts=[HOST],delay=0,retries=1)
            with patch.object(fetcher.direct,"get",side_effect=TransportError("certificate verification",retryable=False)) as request:
                with self.assertRaises(CollectionError):fetcher._request(URL,[HOST])
                self.assertEqual(request.call_count,1)

    def test_proxy_environment_does_not_change_direct_command_and_partial_body_is_rejected(self):
        transport=DirectTransport(5,1024,"test")
        def run(command,**kwargs):
            self.assertEqual(command[:2],["/usr/bin/curl","--disable"])
            self.assertIn("if!en7",command)
            self.assertEqual(command[command.index("--noproxy")+1],"*")
            self.assertNotIn("--insecure",command)
            self.assertNotIn("--location",command)
            Path(command[command.index("--output")+1]).write_bytes(b"partial page")
            return subprocess.CompletedProcess(command,28,'{"http_code":200,"ssl_verify_result":0}',"")
        with patch.dict("os.environ",{"HTTPS_PROXY":"http://127.0.0.1:1"}),patch("campus_radar.network.subprocess.run",side_effect=run):
            with self.assertRaisesRegex(TransportError,"curl 28"):
                transport._curl(URL,"en7",HOST+":443:202.120.2.114",max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
