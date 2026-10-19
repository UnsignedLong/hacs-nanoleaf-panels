"""Exercise the real writer/API code with a simulated Nanoleaf and HA boundary."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'custom_components' / 'nanoleaf_panels'

def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    return obj

class HAError(Exception):
    pass

class ClientError(Exception):
    pass

class NanoleafException(Exception):
    pass

class FakeNanoleaf:
    def __init__(self, session, host, token):
        self.host, self.token = host, token
    async def _request(self, method, path, data):
        return self.host, self.token, method, path

def load_sources():
    package = module('subject')
    package.__path__ = [str(ROOT)]
    stubs = {
        'subject': package,
        'homeassistant': module('homeassistant'),
        'homeassistant.core': module('homeassistant.core', HomeAssistant=object, callback=lambda f: f),
        'homeassistant.const': module('homeassistant.const', CONF_HOST='host', CONF_TOKEN='token'),
        'homeassistant.exceptions': module('homeassistant.exceptions', HomeAssistantError=HAError),
        'homeassistant.helpers': module('homeassistant.helpers'),
        'homeassistant.helpers.aiohttp_client': module('homeassistant.helpers.aiohttp_client', async_get_clientsession=lambda h: object()),
        'aiohttp': module('aiohttp', ClientError=ClientError, ClientResponse=object),
        'aionanoleaf2': module('aionanoleaf2', Nanoleaf=FakeNanoleaf, NanoleafException=NanoleafException),
    }
    with patch.dict(sys.modules, stubs):
        loaded = []
        for name in ['const', 'nanoleaf_api', 'panel_writer']:
            spec = importlib.util.spec_from_file_location('subject.' + name, ROOT / (name + '.py'))
            obj = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = obj
            spec.loader.exec_module(obj)
            loaded.append(obj)
    return loaded

const, api_module, writer_module = load_sources()
RED = (255, 0, 0, 0, 20)
GREEN = (0, 255, 0, 0, 20)
BLUE = (0, 0, 255, 0, 20)

def decode(payload):
    values = list(map(int, payload['write']['animData'].split()))
    state, idx = {}, 1
    for _ in range(values[0]):
        pid, count = values[idx:idx + 2]
        idx += 2
        state[pid] = [tuple(values[idx + k * 5:idx + (k + 1) * 5]) for k in range(count)]
        idx += count * 5
    assert idx == len(values)
    return state

class Hass:
    def __init__(self):
        self.data = {}
        self.parent_state = 'off'
        self.entry = types.SimpleNamespace(data={'host': 'old', 'token': 'one'})
        self.states = types.SimpleNamespace(get=lambda _: types.SimpleNamespace(state=self.parent_state))
        self.config_entries = types.SimpleNamespace(async_get_entry=lambda _: self.entry)
    def async_create_task(self, coro, name=None):
        return asyncio.create_task(coro, name=name)

class Device:
    host = 'simulated'
    def __init__(self):
        self.writes = []
        self.stale = {pid: const.BLACK for pid in range(1, 10)}
        self.fail = False
        self.started = asyncio.Event()
        self.release = None
    async def async_get_device_info(self):
        return {'effects': {'selectedEffect': '*Static*'}}
    async def async_get_current_panel_colors(self):
        return dict(self.stale)
    async def async_write_effects(self, payload):
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            raise HAError('device unavailable')
        self.writes.append(payload)

class WriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass, self.device = Hass(), Device()
        self.writer = writer_module.PanelWriter(self.hass, self.device, 'entry')
        self.writer.panel_ids = list(range(1, 10))
        self.writer.nanoleaf_entity_id = 'light.parent'
    async def test_parallel_nine_panels_coalesce_without_erasing(self):
        await asyncio.gather(*(self.writer.async_write({pid: [RED]}, blank_others_when_off=True) for pid in range(1, 10)))
        self.assertEqual(len(self.device.writes), 1)
        self.assertTrue(all(frames == [RED] for frames in decode(self.device.writes[-1]).values()))
    async def test_sequential_calls_preserve_prior_write_despite_stale_readback(self):
        await self.writer.async_write({1: [RED]}, blank_others_when_off=True)
        await self.writer.async_write({2: [GREEN]}, blank_others_when_off=True)
        state = decode(self.device.writes[-1])
        self.assertEqual(state[1], [RED])
        self.assertEqual(state[2], [GREEN])
        self.assertEqual(state[3], [const.BLACK])
    async def test_calls_during_inflight_write_form_serialized_next_batch(self):
        self.device.release = asyncio.Event()
        first = asyncio.create_task(self.writer.async_write({1: [RED]}, blank_others_when_off=True))
        await self.device.started.wait()
        second = asyncio.create_task(self.writer.async_write({2: [GREEN]}, blank_others_when_off=True))
        await asyncio.sleep(0.06)
        self.assertEqual(len(self.device.writes), 0)
        self.device.release.set()
        await asyncio.gather(first, second)
        self.assertEqual(len(self.device.writes), 2)
        self.assertEqual(decode(self.device.writes[-1])[1], [RED])
        self.assertEqual(decode(self.device.writes[-1])[2], [GREEN])
    async def test_failed_batch_not_cached_and_all_callers_get_error_then_recover(self):
        self.device.fail = True
        results = await asyncio.gather(self.writer.async_write({1: [RED]}), self.writer.async_write({2: [GREEN]}), return_exceptions=True)
        self.assertTrue(all(isinstance(r, HAError) for r in results))
        self.assertFalse(self.writer.recently_written)
        self.assertEqual(writer_module.get_cached_panel_state(self.hass, 'entry'), {})
        self.device.fail = False
        await self.writer.async_write({3: [BLUE]})
        self.assertEqual(decode(self.device.writes[-1])[3], [BLUE])
    async def test_cancelled_caller_does_not_cancel_shared_batch(self):
        first = asyncio.create_task(self.writer.async_write({1: [RED]}))
        second = asyncio.create_task(self.writer.async_write({2: [GREEN]}))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertEqual(decode(self.device.writes[-1])[1], [RED])
        self.assertEqual(decode(self.device.writes[-1])[2], [GREEN])
    async def test_off_then_on_blanks_other_panels(self):
        await self.writer.async_write({1: [RED]}, blank_others_when_off=True)
        self.writer.note_device_off()
        await self.writer.async_write({2: [GREEN]}, blank_others_when_off=True)
        self.assertEqual(decode(self.device.writes[-1])[1], [const.BLACK])
    async def test_animation_and_listener_state(self):
        notifications = []
        remove = self.writer.async_add_listener(notifications.append)
        await self.writer.async_write({1: [RED, BLUE]})
        payload = self.device.writes[-1]['write']
        self.assertEqual(payload['animType'], 'custom')
        self.assertTrue(payload['loop'])
        self.assertEqual(decode(self.device.writes[-1])[1], [RED, BLUE])
        self.assertEqual(notifications[-1][1], RED)
        remove()
    async def test_readback_becomes_authoritative_after_grace(self):
        await self.writer.async_write({1: [RED]})
        self.writer._last_write -= writer_module.READBACK_GRACE + 1
        self.device.stale[1] = BLUE
        await self.writer.async_write({2: [GREEN]})
        self.assertEqual(decode(self.device.writes[-1])[1], [BLUE])
    async def test_device_lock_blocks_poll_and_write_interleaving(self):
        async with self.writer.lock:
            pending = asyncio.create_task(self.writer.async_write({1: [RED]}))
            await asyncio.sleep(0.06)
            self.assertFalse(self.device.started.is_set())
        await pending
        self.assertEqual(len(self.device.writes), 1)

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_and_token_follow_entry_before_next_request(self):
        hass = Hass()
        client = api_module.NanoleafApiClient(hass, 'entry', 'old', 'one')
        self.assertEqual((await client._async_request('get', ''))[:2], ('old', 'one'))
        original = client._nanoleaf
        await client._async_request('get', '')
        self.assertIs(client._nanoleaf, original)
        hass.entry.data.update(host='new', token='two')
        self.assertEqual((await client._async_request('get', ''))[:2], ('new', 'two'))
        self.assertIsNot(client._nanoleaf, original)
    async def test_library_error_includes_empty_exception_name_and_cause(self):
        hass = Hass()
        client = api_module.NanoleafApiClient(hass, 'entry', 'old', 'one')
        async def fail(*args):
            raise NanoleafException() from ClientError('connection failed')
        client._nanoleaf._request = fail
        with self.assertRaisesRegex(HAError, 'old.*NanoleafException.*ClientError: connection failed'):
            await client._async_request('get', '')

if __name__ == '__main__':
    unittest.main(verbosity=2)
