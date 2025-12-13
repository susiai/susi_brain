"""Shared process-local Chat/TTS accounting and measured model selection."""

import hashlib
import math


def endpoint_id(endpoint):
    return 'ep-' + hashlib.sha256(endpoint.url.encode()).hexdigest()[:16]


class Connection:
    """Release one reservation exactly once, including when cleanup raises."""

    def __init__(self, models, model):
        self.models, self.key, self.released = models, model.casefold(), False
        entry = models.setdefault(self.key, {'model': model, 'count': 0})
        entry['count'] += 1

    def release(self):
        if not self.released:
            self.released = True
            entry = self.models[self.key]
            entry['count'] -= 1
            if entry['count'] == 0:
                del self.models[self.key]


class ChatRouting:
    def __init__(self, settings, endpoints):
        self.settings, self.endpoints = settings, endpoints
        self.active = {endpoint.url: {} for endpoint in endpoints}
        self.measurements = {}

    def snapshot(self):
        endpoints = [{
            'id': endpoint_id(endpoint), 'url': endpoint.url,
            'active_connections': self.count(endpoint),
            'models': {entry['model']: entry['count'] for entry in self.active[endpoint.url].values()},
        } for endpoint in self.endpoints]
        return {'storage': 'process-local',
                'active_connections': sum(endpoint['active_connections'] for endpoint in endpoints),
                'endpoints': endpoints}

    def count(self, endpoint):
        return sum(entry['count'] for entry in self.active[endpoint.url].values())

    def reserve(self, endpoint, model):
        return Connection(self.active[endpoint.url], model)

    def speech_expected(self, endpoint, model, voice):
        records = self.measurements.get(endpoint_id(endpoint), {})
        record = next((value for name, value in records.items() if name.casefold() == model.casefold()), {})
        metrics = record.get('metrics') or {}
        if record.get('task') != 'speech' or record.get('voice') != voice:
            metrics = {}
        return {name: (value if type(value := metrics.get(name)) in (int, float)
                      and math.isfinite(value) and value >= 0 else None)
                for name in ('ttfa_seconds', 'real_time_factor')}

    def select_speech(self, candidates, voice):
        """Reserve the least-loaded compatible endpoint, then prefer measured speed."""
        def key(item):
            endpoint, model = item
            metrics = self.speech_expected(endpoint, model, voice)
            rtf, ttfa = metrics['real_time_factor'], metrics['ttfa_seconds']
            return (self.count(endpoint), rtf is None, rtf or 0, ttfa is None, ttfa or 0)
        if not candidates:
            return None
        endpoint, model = min(candidates, key=key)
        return endpoint, model, self.reserve(endpoint, model)

    def expected(self, endpoint, model):
        """Copy valid calibration metrics for this exact endpoint/model pair."""
        records = self.measurements.get(endpoint_id(endpoint), {})
        record = next((record for name, record in records.items() if name.casefold() == model.casefold()), {})
        metrics = record.get('metrics') or {}

        def measured(name, *, positive=False):
            value = metrics.get(name)
            return (value if type(value) in (int, float) and math.isfinite(value)
                    and (value > 0 if positive else value >= 0) else None)

        rate = measured('tokens_per_second', positive=True)
        ttft = measured('ttft_seconds')
        return {'tokens_per_second': rate, 'ttft_seconds': ttft}

    def _speed(self, endpoint, model):
        metrics = self.expected(endpoint, model)
        rate, ttft = metrics['tokens_per_second'], metrics['ttft_seconds']
        return (rate is None, -rate if rate is not None else 0,
                ttft is None, ttft if ttft is not None else 0)

    def select(self, group, inventories):
        """Select and reserve without awaiting, so concurrent requests see the reservation."""
        candidates = []
        configured_models = self.settings.chat_models(group)
        for endpoint_index, (endpoint, inventory) in enumerate(zip(self.endpoints, inventories)):
            advertised = {name.casefold(): name for name in reversed(inventory)}
            models = [(index, model, advertised[model.name.casefold()])
                      for index, model in enumerate(configured_models)
                      if model.name.casefold() in advertised]
            # Reuse a running model in this group instead of loading another on the same server.
            running = [item for item in models if item[2].casefold() in self.active[endpoint.url]]
            load = self.count(endpoint)
            for model_index, configured, model in running or models:
                key = (load, -configured.ranking if group == 'smart' else 0,
                       *self._speed(endpoint, model), model_index, endpoint_index)
                candidates.append((key, endpoint, model))
        if not candidates:
            return None
        _, endpoint, model = min(candidates, key=lambda item: item[0])
        return endpoint, model, self.reserve(endpoint, model)
