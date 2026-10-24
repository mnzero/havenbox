"""Synthetic registries exercise contract logic, never product acceptance."""
import copy
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from contracts.baseline import (
    ROOT,
    ContractError,
    authorize_ordinary,
    authorize_validation,
    binding,
    check_inventory,
    check_schema,
    digest,
    load,
    normalize_parameters,
    validate,
)


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.manifest = load(ROOT / 'config/runtime-manifest.json')
        self.matrix = load(ROOT / 'config/compatibility-matrix.json')
        self.parameters = load(ROOT / 'config/inference-parameters.json')

    def test_checked_in_inventory(self):
        check_inventory(self.manifest, self.matrix)
        check_schema(self.parameters, 'inference-parameter')

    def test_schema_negative_cases(self):
        for field, bad in [('schemaVersion', 'future'), ('state', 'ready'),
                           ('unexpected', True), ('dependencies', []), ('limits', {})]:
            with self.subTest(field=field):
                value = copy.deepcopy(self.manifest)
                value[field] = bad
                with self.assertRaises(ContractError):
                    check_schema(value, 'runtime-manifest')
        for bad in (True, -1, 0, '12', float('nan')):
            value = copy.deepcopy(self.manifest)
            value['limits']['requestsPerTurn'] = bad
            with self.assertRaises(ContractError):
                check_schema(value, 'runtime-manifest')

    def test_parser_and_schema_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data.json'
            for text in ('{"a":1,"a":2}', '{"n":NaN}', '{', '{"n":Infinity}'):
                path.write_text(text)
                with self.assertRaises(ContractError):
                    load(path)
        with self.assertRaises(ContractError):
            validate({}, {'unsupported': True})

    def test_domain_ids_and_route_union(self):
        value = {'kind': 'Turn', 'id': 'turn_1', 'sessionId': 'session_1',
                 'sequence': 1, 'route': {'kind': 'Local', 'resourceRevision': 'r1',
                                        'runtimeRevision': 'rt1', 'modelId': 'test'}}
        check_schema(value, 'domain')
        for key, bad in [('id', 'task_1'), ('route', {'kind': 'Online', 'modelId': 'x'})]:
            mutated = copy.deepcopy(value)
            mutated[key] = bad
            with self.assertRaises(ContractError):
                check_schema(mutated, 'domain')

    def test_all_candidates_and_unknown_denied(self):
        for cid in [c['id'] for c in self.matrix['combinations']] + ['unknown']:
            with self.assertRaises(ContractError):
                authorize_ordinary(self.manifest, self.matrix, cid, {})

    def test_builtin_candidate_and_qualification(self):
        # Synthetic candidate, not a claim of a verified builtin/model pair.
        c = self.matrix['combinations'][0]
        c['harness'] = 'builtin'
        check_schema(self.matrix, 'compatibility-matrix')
        with self.assertRaisesRegex(ContractError, 'baseline not frozen'):
            authorize_ordinary(self.manifest, self.matrix, c['id'], {})
        self.manifest['state'] = 'frozen'
        with self.assertRaisesRegex(ContractError, 'unverified combination'):
            authorize_ordinary(self.manifest, self.matrix, c['id'], {})
        self.manifest['state'] = 'candidate'
        grant, request, kwargs = self.grant_fixture()
        self.assertIn('qualification-only', authorize_validation(
            self.manifest, self.matrix, grant, request, **kwargs))
        with self.assertRaisesRegex(ContractError, 'disabled in this build'):
            authorize_validation(self.manifest, self.matrix, grant, request,
                                 **dict(kwargs, build_flavor='release'))
        c['harness'] = 'unknown-builtin'
        with self.assertRaises(ContractError):
            check_schema(self.matrix, 'compatibility-matrix')

    def resolved_combination(self):
        c = self.matrix['combinations'][0]
        c['adapterRevision'] = 'synthetic-adapter-v1'
        c['route'].update(providerRevision='synthetic-provider-v1', credentialVersion='test-key-v1',
                          endpointRevision='test-endpoint-v1', protocol='test-protocol', modelId='test-model')
        return c

    def grant_fixture(self):
        c = self.resolved_combination()
        for dep in self.manifest['dependencies']:
            dep.update(version='synthetic-v1', sha256='0' * 64)
        scope = {'combinationId': c['id'], 'datasetDigest': digest('synthetic CSV'),
                 'routeDigest': digest(c['route']), 'epoch': 1, 'sessionId': 'session_1',
                 'turnId': 'turn_1', 'authorizationDigest': digest('explicit test consent')}
        limits = {'requests': 2, 'inputTokens': 100, 'outputTokens': 50,
                  'bytes': 1024, 'toolCalls': 2, 'seconds': 60}
        grant = dict(schemaVersion='hb.validation-grant.v1', id='test-only-not-a-token',
                     binding=binding(self.manifest, c), **scope, notBefore=100, expiresAt=200,
                     limits=limits, purpose='synthetic-validation-only')
        request = dict(scope, reservation=dict.fromkeys(limits, 1))
        kwargs = {'build_flavor': 'internal-validation',
                  'issued_grants': {grant['id']: copy.deepcopy(grant)},
                  'synthetic_datasets': {scope['datasetDigest']}, 'now': 150}
        return grant, request, kwargs

    def test_grant_positive_and_boundaries(self):
        grant, request, kwargs = self.grant_fixture()
        self.assertIn('qualification-only', authorize_validation(self.manifest, self.matrix, grant, request, **kwargs))
        for now in (99, 200, 201):
            with self.assertRaises(ContractError):
                authorize_validation(self.manifest, self.matrix, grant, request, **dict(kwargs, now=now))
        for flavor in ('release', 'ordinary', 'debug', ''):
            with self.assertRaises(ContractError):
                authorize_validation(self.manifest, self.matrix, grant, request, **dict(kwargs, build_flavor=flavor))
        for key in ('issued_grants', 'synthetic_datasets'):
            with self.assertRaises(ContractError):
                authorize_validation(self.manifest, self.matrix, grant, request, **dict(kwargs, **{key: {}}))

    def test_grant_scope_and_limits(self):
        grant, request, kwargs = self.grant_fixture()
        for key in ('combinationId', 'datasetDigest', 'routeDigest', 'epoch', 'sessionId', 'turnId', 'authorizationDigest'):
            bad = copy.deepcopy(request)
            bad[key] = 2 if key == 'epoch' else ('f' * 64 if key.endswith('Digest') else 'different')
            with self.subTest(key=key), self.assertRaises(ContractError):
                authorize_validation(self.manifest, self.matrix, grant, bad, **kwargs)
        for key in grant['limits']:
            bad = copy.deepcopy(request)
            bad['reservation'][key] = grant['limits'][key] + 1
            with self.subTest(key=key), self.assertRaises(ContractError):
                authorize_validation(self.manifest, self.matrix, grant, bad, **kwargs)
        changed = copy.deepcopy(grant)
        changed['expiresAt'] = 5000
        with self.assertRaises(ContractError):
            authorize_validation(self.manifest, self.matrix, changed, request, **kwargs)
        kwargs['issued_grants'][changed['id']] = changed
        with self.assertRaises(ContractError):
            authorize_validation(self.manifest, self.matrix, changed, request, **kwargs)

    def test_drift_invalidates_grant(self):
        grant, request, kwargs = self.grant_fixture()
        mutations = [('revision', 'new'), ('recovery', 'changed')]
        for field, value in mutations:
            changed = copy.deepcopy(self.manifest)
            changed[field] = value
            with self.assertRaises(ContractError):
                authorize_validation(changed, self.matrix, grant, request, **kwargs)
        self.matrix['combinations'][0]['route']['credentialVersion'] = 'new-key'
        with self.assertRaises(ContractError):
            authorize_validation(self.manifest, self.matrix, grant, request, **kwargs)

    def test_missing_inventory_rejected(self):
        for field in ('contracts', 'dependencies'):
            changed = copy.deepcopy(self.manifest)
            changed[field].pop()
            with self.assertRaises(ContractError):
                check_inventory(changed, self.matrix)

    def test_unpinned_candidate_grant_rejected(self):
        grant, request, kwargs = self.grant_fixture()
        self.manifest['dependencies'][0]['sha256'] = None
        grant['binding'] = binding(self.manifest, self.matrix['combinations'][0])
        kwargs['issued_grants'][grant['id']] = copy.deepcopy(grant)
        with self.assertRaisesRegex(ContractError, 'assets not pinned'):
            authorize_validation(self.manifest, self.matrix, grant, request, **kwargs)

    def test_contract_asset_drift(self):
        self.manifest['contracts'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ContractError, 'contract drift'):
            check_inventory(self.manifest, self.matrix)

    def test_source_snapshot_drift(self):
        for field, value in [('sha256', '0' * 64), ('baseCommit', '1' * 40),
                             ('paths', self.manifest['sourceSnapshot']['paths'][:-1])]:
            changed = copy.deepcopy(self.manifest)
            changed['sourceSnapshot'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ContractError, 'source snapshot'):
                check_inventory(changed, self.matrix)

    @contextmanager
    def private_source(self):
        # Copy actual checked-in inputs, not a synthetic snapshot dictionary.
        paths = set(self.manifest['sourceSnapshot']['paths'])
        paths.update(item['path'] for item in self.manifest['contracts'])
        paths.update(['config/runtime-manifest.json', 'config/compatibility-matrix.json',
                      '.github/workflows/engineering.yml'])
        # Keep these explicit so an inventory omission cannot hide the regression.
        paths.add('tests/RuntimeTests/RuntimeTests.swift')
        paths.update(f'guest/{crate}/Cargo.toml' for crate in
                     ('protocol', 'adapters', 'supervisor', 'csv-engine'))
        with tempfile.TemporaryDirectory(prefix='hb09-regression-') as directory:
            root = Path(directory).resolve()
            for relative in paths:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, destination)
            with patch('contracts.baseline.ROOT', root):
                yield root

    def test_actual_crate_manifest_drift(self):
        with self.private_source() as root:
            check_inventory(self.manifest, self.matrix)
            for crate in ('protocol', 'adapters', 'supervisor', 'csv-engine'):
                with self.subTest(crate=crate):
                    path = root / f'guest/{crate}/Cargo.toml'
                    original = path.read_bytes()
                    self.assertIn(b'edition.workspace = true', original)
                    try:
                        path.write_bytes(original.replace(b'edition.workspace = true',
                                                          b'edition = "2021"'))
                        with self.assertRaisesRegex(ContractError, 'source snapshot content drift'):
                            check_inventory(self.manifest, self.matrix)
                    finally:
                        path.write_bytes(original)
                    check_inventory(self.manifest, self.matrix)

    def test_actual_csv_engine_drift(self):
        for relative in ('guest/csv-engine/src/lib.rs', 'guest/csv-engine/src/spill.rs',
                         'guest/csv-engine/tests/acceptance.rs', 'guest/csv-engine/schema.json'):
            self.assertIn(relative, self.manifest['sourceSnapshot']['paths'])
            with self.private_source() as root:
                check_inventory(self.manifest, self.matrix)
                path = root / relative
                path.write_bytes(path.read_bytes() + b'\n ')
                with self.assertRaisesRegex(ContractError, 'source snapshot content drift'):
                    check_inventory(self.manifest, self.matrix)

    def test_actual_runtime_test_drift(self):
        relative = 'tests/RuntimeTests/RuntimeTests.swift'
        self.assertIn(relative, self.manifest['sourceSnapshot']['paths'])
        with self.private_source() as root:
            check_inventory(self.manifest, self.matrix)
            path = root / relative
            original = path.read_bytes()
            try:
                path.write_bytes(original + b'\n// HB-11 actual test drift regression\n')
                with self.assertRaisesRegex(ContractError, 'source snapshot content drift'):
                    check_inventory(self.manifest, self.matrix)
            finally:
                path.write_bytes(original)
            check_inventory(self.manifest, self.matrix)

    def test_actual_ci_checkout_policy(self):
        with self.private_source() as root:
            path = root / '.github/workflows/engineering.yml'
            original = path.read_text()
            check_inventory(self.manifest, self.matrix)
            mutations = [
                ('ref: ${{ github.sha }}', f'ref: {self.manifest["sourceCommit"]}'),
                ('ref: ${{ github.sha }}', 'ref: 11bd71901bbe5b1630ceea73d27597364c9af683'),
                ('actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683',
                 'actions/checkout@v4'),
            ]
            for before, after in mutations:
                with self.subTest(replacement=after):
                    self.assertIn(before, original)
                    try:
                        path.write_text(original.replace(before, after))
                        with self.assertRaisesRegex(ContractError, 'CI checkout policy'):
                            check_inventory(self.manifest, self.matrix)
                    finally:
                        path.write_text(original)
                    check_inventory(self.manifest, self.matrix)

    def test_trusted_evidence_and_version_drift(self):
        # Entirely fabricated fixtures, never persisted to the product matrix.
        c = self.resolved_combination()
        self.manifest['state'] = 'frozen'
        self.manifest['unresolved'] = []
        self.manifest['platform']['minimumOSVerified'] = True
        self.manifest['platform']['referenceHardware'] = 'synthetic-device'
        self.manifest['sqlite']['version'] = 'test-v1'
        for dep in self.manifest['dependencies']:
            dep.update(version='test-v1', sha256='0'*64, license='test-only', sbom='test-sbom')
        c['state'] = 'validated'
        c['evidence'] = {gate: 'test-' + gate for gate in ('functional', 'security', 'release')}
        records = {receipt: {'binding': binding(self.manifest, c), 'gate': gate, 'result': 'pass'}
                   for gate, receipt in c['evidence'].items()}
        authorize_ordinary(self.manifest, self.matrix, c['id'], records)
        for gate in c['evidence']:
            bad = copy.deepcopy(records)
            bad[c['evidence'][gate]]['result'] = 'unknown'
            with self.assertRaises(ContractError):
                authorize_ordinary(self.manifest, self.matrix, c['id'], bad)
        for field in ('version', 'sha256', 'license', 'sbom'):
            changed = copy.deepcopy(self.manifest)
            changed['dependencies'][0][field] = '1'*64 if field == 'sha256' else 'changed'
            with self.assertRaises(ContractError):
                authorize_ordinary(changed, self.matrix, c['id'], records)

    def assert_invalid_token_rule(self, schema, key):
        with self.assertRaisesRegex(ContractError, key):
            check_schema(schema, 'inference-parameter')
        # Overrides must not hide an invalid contract/default, nor yield KeyError.
        for supplied in ({}, {key: 1}):
            with self.assertRaisesRegex(ContractError, key):
                normalize_parameters(schema, supplied, input_tokens=0,
                                     template_tokens=0, tool_tokens=0)

    def test_token_rule_exact_regressions(self):
        for key in ('contextTokens', 'maxOutputTokens'):
            with self.subTest(key=key):
                schema = copy.deepcopy(self.parameters)
                schema['fields'][key]['supported'] = False
                self.assert_invalid_token_rule(schema, key)
        schema = copy.deepcopy(self.parameters)
        schema['fields']['maxOutputTokens'].update(
            default=0.5, valueSchema={'type': 'number', 'minimum': 0, 'maximum': 2048})
        self.assert_invalid_token_rule(schema, 'maxOutputTokens')

    def test_token_rule_types_and_bounds(self):
        for key in ('contextTokens', 'maxOutputTokens'):
            for field in ('default', 'minimum', 'maximum'):
                for bad in (False, True, 0, -1, 0.5, 1.0, '1', None, float('nan'), float('inf')):
                    with self.subTest(key=key, field=field, bad=bad):
                        schema = copy.deepcopy(self.parameters)
                        rule = schema['fields'][key]
                        target = rule if field == 'default' else rule['valueSchema']
                        target[field] = bad
                        self.assert_invalid_token_rule(schema, key)
            for bounds, default in [((3, 2), 2), ((2, 4), 1), ((2, 4), 5)]:
                with self.subTest(key=key, bounds=bounds, default=default):
                    schema = copy.deepcopy(self.parameters)
                    schema['fields'][key].update(default=default, valueSchema={
                        'type': 'integer', 'minimum': bounds[0], 'maximum': bounds[1]})
                    self.assert_invalid_token_rule(schema, key)
            for value_schema in ({'type': 'number', 'minimum': 1, 'maximum': 8},
                                 {'type': 'string'}, {'enum': ['1', '8']}):
                schema = copy.deepcopy(self.parameters)
                schema['fields'][key]['valueSchema'] = value_schema
                self.assert_invalid_token_rule(schema, key)

    def test_token_positive_integer_boundaries(self):
        schema = copy.deepcopy(self.parameters)
        for key in ('contextTokens', 'maxOutputTokens'):
            schema['fields'][key].update(default=1, valueSchema={
                'type': 'integer', 'minimum': 1, 'maximum': 4})
        check_schema(schema, 'inference-parameter')
        for n in (1, 4):
            result = normalize_parameters(schema, {'contextTokens': n, 'maxOutputTokens': n},
                                          input_tokens=0, template_tokens=0, tool_tokens=0)
            self.assertEqual(result['effective']['maxOutputTokens'], n)
        for key in ('contextTokens', 'maxOutputTokens'):
            for bad in (True, 0, -1, 0.5, '1', 5):
                with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                    normalize_parameters(schema, {key: bad}, input_tokens=0,
                                         template_tokens=0, tool_tokens=0)
        for key in ('contextTokens', 'maxOutputTokens'):
            schema['fields'][key]['default'] = 4
            schema['fields'][key]['valueSchema']['minimum'] = 4
        check_schema(schema, 'inference-parameter')
        self.assertEqual(normalize_parameters(schema, {}, input_tokens=0,
                         template_tokens=0, tool_tokens=0)['effective']['contextTokens'], 4)
        with self.assertRaisesRegex(ContractError, 'context budget exceeded'):
            normalize_parameters(schema, {}, input_tokens=1, template_tokens=0, tool_tokens=0)

    def test_parameters_and_context_accounting(self):
        def normalize(supplied, n=100):
            return normalize_parameters(self.parameters, supplied, input_tokens=n,
                                        template_tokens=100, tool_tokens=100)
        self.assertEqual(normalize({})['effective']['maxOutputTokens'], 2048)
        for bad in ({'unknown': 1}, {'temperature': 0.5}, {'reasoningEffort': 'high'},
                    {'maxOutputTokens': True}, {'contextTokens': 9000}, {'maxOutputTokens': -1}):
            with self.assertRaises(ContractError):
                normalize(bad)
        with self.assertRaises(ContractError):
            normalize({}, 8000)


if __name__ == '__main__':
    unittest.main()
