"""Regression coverage for credential handling and safe output publication."""
import asyncio
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import output_writer as ow
import diff_runs
import puppeteer_scraper as puppeteer
from proxy_pool import Proxy, ProxyParseError, parse_proxy_line


class AuditSecurity(unittest.TestCase):
    def test_invalid_proxy_never_echoes_credentials(self):
        for value in ('http://fake:fake:password@127.0.0.1:8080', '127.0.0.1:8080:fake:fake:password'):
            with self.assertRaises(ProxyParseError) as caught:
                parse_proxy_line(value)
            self.assertNotIn('fake', str(caught.exception))
            self.assertNotIn('password@', str(caught.exception))

    def test_auth_only_for_matching_proxy_and_only_once(self):
        class Client:
            def __init__(self): self.events, self.calls = {}, []
            def on(self, name, handler): self.events[name] = handler
            async def send(self, name, payload): self.calls.append((name, payload))
        class Page:
            def __init__(self): self._client = Client()
        async def run():
            page = Page()
            await puppeteer._authenticate_if_needed(page, Proxy('127.0.0.1', 8080, 'fake', 'fake_password'))
            challenges = [
                ('server', 'Server', 'http://127.0.0.1:8080'),
                ('other', 'Proxy', 'http://127.0.0.2:8080'),
                ('bad-port', 'Proxy', 'http://127.0.0.1:broken'),
                ('missing', None, 'http://127.0.0.1:8080'),
                ('proxy', 'Proxy', 'http://127.0.0.1:8080'),
                ('proxy', 'Proxy', 'http://127.0.0.1:8080'),
            ]
            for request, source, origin in challenges:
                page._client.events['Fetch.authRequired']({'requestId': request,
                    'authChallenge': {'source': source, 'origin': origin}})
                await asyncio.sleep(0)
            return [data['authChallengeResponse'] for name, data in page._client.calls if name == 'Fetch.continueWithAuth']
        replies = asyncio.run(run())
        self.assertEqual([r['response'] for r in replies], ['CancelAuth'] * 4 + ['ProvideCredentials', 'CancelAuth'])
        self.assertEqual(replies[4]['password'], 'fake_password')
        self.assertTrue(all('password' not in r for r in replies if r['response'] == 'CancelAuth'))


class AuditOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def row(self, title):
        return ow.Product(sku='fake', source='facebook.com', category='test', title=title,
            brand=None, price=None, currency=None, price_source=None, product_url=None,
            image_url=None, scraped_at='2026-10-06T00:00:00Z')

    def test_failed_json_write_preserves_previous_file(self):
        target = self.root / 'result.json'
        target.write_text('[{"old":true}]')
        def fail(rows, stream, **kwargs):
            stream.write('[')
            raise OSError('simulated full disk')
        with patch.object(ow.json, 'dump', fail):
            with self.assertRaises(OSError): ow.write_json([self.row('hello')], str(target))
        self.assertEqual(json.loads(target.read_text()), [{'old': True}])
        self.assertEqual(list(self.root.iterdir()), [target])

    def test_failed_replace_preserves_previous_file(self):
        for fmt in ('json', 'csv'):
            target = self.root / ('result.' + fmt)
            target.write_text('previous')
            with patch.object(ow.os, 'replace', side_effect=OSError('simulated replace error')):
                with self.assertRaises(OSError): ow.write_output([self.row('hello')], str(target), fmt)
            self.assertEqual(target.read_text(), 'previous')
            self.assertFalse(list(self.root.glob('*.tmp')))

    def test_csv_formula_escape_and_monitoring_round_trip(self):
        titles = ['=1+1', '+123', '-123', '@SUM(1)', '  =1+1', '\t=1+1', "'literal", 'ordinary']
        rows = [self.row(title) for title in titles]
        target = self.root / 'result.csv'
        ow.finish_run(products=rows, out_path=str(target), fmt='csv', engine='test', url='test',
            pages_requested=1, pages_completed=1, failed_pages=[], blocked=False,
            remote_api_error=False, allow_empty=False, started_at=0)
        with target.open(newline='') as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual([r['title'] for r in exported], ["'" + title for title in titles[:-1]] + ['ordinary'])
        self.assertEqual([r['title'] for r in diff_runs._load_rows(str(target))], titles)

    def test_failed_metadata_write_preserves_previous_sidecar(self):
        target = self.root / 'result.json'
        sidecar = Path(str(target) + '.meta.json')
        sidecar.write_text('{"old":true}')
        with patch.object(ow.json, 'dump', side_effect=OSError('simulated full disk')):
            with self.assertRaises(OSError):
                ow.write_meta(str(target), status='complete', stop_reason='complete', engine='test',
                    url='test', pages_requested=1, pages_completed=1, product_count=1, started_at=0)
        self.assertEqual(json.loads(sidecar.read_text()), {'old': True})
        self.assertEqual(list(self.root.iterdir()), [sidecar])


class AuditPageInputs(unittest.TestCase):
    def test_dotted_username_matches_full_url(self):
        import page_parser as pp
        for name in ('abc.def', '@abc.def', 'brand.tv', 'some.name'):
            self.assertEqual(pp.normalize_input(name), pp.normalize_input('https://www.facebook.com/' + name.lstrip('@')))
        self.assertIsNone(pp.normalize_input('facebook.com'))
        self.assertIsNone(pp.normalize_input('https://example.com/abc.def'))
