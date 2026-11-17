// Optional Node-only contract test: no Chrome, Google session, or API calls.
import assert from 'node:assert/strict';
import test from 'node:test';
import { fieldOf, load, propertyRuleCombiner, schemaOf } from './native.mjs';

const convert = load('switching.py', '_CONVERT');

async function run({ invalid = false, mutate = false, throwConversion = false, failDispose = false,
  properties = [], unknownField = false, ignoreWrite = false, enabled = true,
  targetType = 'simple-table' } = {}) {
  const disposed = [];
  const shared = { value: 'original' };
  const config = { id: 'original', type: 'simple-combochart', preset: 'default' };
  const original = { getId: () => 'original', getConfig: () => config };
  let drafts = 0;
  const owner = {
    isReady$: { value: true },
    getVersion: () => '1',
    isResponsiveLayout: () => false,
    getComponents: () => [original],
    getConfig: () => shared,
    createChildComponent: cfg => {
      const index = ++drafts;
      return {
        getConfig: () => cfg,
        getReadyAsync: async () => {},
        syncState() {},
        setBehavior: (key, value) => { cfg.behavior = { ...(cfg.behavior || {}), [key]: value }; },
        validate() {},
        getConfigErrors: () => invalid && !cfg.bindings ? { fields: { invalid: true } } : {},
        getSchema: () => ({ getConcepts() {}, ...schemaOf(() => fieldOf(!unknownField)) }),
        getDisplayTypeObj: () => ({
          getCustomBehavior: (_config, key) => {
            assert.equal(key, 'onSelect');
            return [{ action: 'lego-dimension-filter', isControl: true }];
          },
          getPropertyInputConfig: () => ({
          filterMetric: { disabled: true, bindingMethods: {
            bindingKeys: [{ type: 'datasource', key: 'metrics' }],
            readFromConfig: () => [],
            writeToConfig: () => { throw Error('Disabled empty metric must not be written'); },
          } },
          [targetType === 'dimension-filter' ? 'controlField' : 'actionConceptList']: { enabledBy: { callback: () => enabled }, bindingMethods: {
            bindingKeys: [{ type: 'datasource', key: 'dimensions' }],
            readFromConfig: () => cfg.bindings || [],
            writeToConfig: (_wrapper, event) => { if (!ignoreWrite) cfg.bindings = event.value; },
          } },
        }) }),
        dispose() {
          disposed.push(index);
          if (failDispose && index === 2) throw Error('dispose failed');
        },
      };
    },
  };
  const cache = { ma: {} };
  const services = {
    reportContextService: { getReport: () => ({ getId: () => 'report', getPage: () => owner, getConfig: () => ({}) }) },
    legoMainRegistry: {
      getDisplayTypeObj: () => ({ getPresetConfigNames: () => ['default'] }),
      getDisplayTypeGroup: () => 'simple',
    },
    vizSwitchService: { cache, queues: {} },
    globalVariableService: cache,
  };
  function serializer(page) {
    // Structural discovery markers: componentConfig:[],groupConfig:[],conceptDefs:[] hiddenFromViewer pageId:
    return { page: { componentConfig: page.component.map(c => ({ componentId: c.id, type: c.type, preset: c.preset,
      ...(c.behavior ? { behavior: c.behavior } : {}) })) } };
  }
  globalThis.document = { body: {} };
  globalThis.window = {
    angular: { element: () => ({ injector: () => ({ get: name => services[name] }) }) },
    default_datastudio: { serializer, propertyRuleCombiner },
  };
  try {
    const result = await convert.call((_service, draft, type, preset) => {
      if (mutate) shared.value = 'changed';
      if (throwConversion) throw Error('converter failed');
      return { ...draft.getConfig(), type, preset };
    }, {
      reportId: 'report', pageId: 'page', version: '1', timeout: 1000,
      changes: [{ component_id: 'original', type: targetType, preset: 'default', properties }],
    });
    return { result, disposed };
  } finally {
    delete globalThis.document;
    delete globalThis.window;
  }
}

test('invalid draft still checks shared state after disposing both drafts', async () => {
  assert.deepEqual(await run({ invalid: true }), {
    result: { error: 'INVALID_COMPONENT_CONFIGURATION' }, disposed: [2, 1],
  });
  assert.deepEqual(await run({ invalid: true, mutate: true }), {
    result: { error: 'UNEXPECTED_SIDE_EFFECT' }, disposed: [2, 1],
  });
});

test('converter exception still checks shared state and disposes its source draft', async () => {
  assert.deepEqual(await run({ throwConversion: true, mutate: true }), {
    result: { error: 'UNEXPECTED_SIDE_EFFECT' }, disposed: [1],
  });
});

test('target disposal failure does not skip source disposal or claim success', async () => {
  assert.deepEqual(await run({ failDispose: true }), {
    result: { error: 'NATIVE_CONTRACT_FAILED' }, disposed: [2, 1],
  });
});

test('successful isolated conversion returns edits and verifies shared state', async () => {
  const { result, disposed } = await run();
  assert.deepEqual(disposed, [2, 1]);
  assert.equal(result.shared_state_unchanged, true);
  assert.equal(result.edits[0].before.type, 'simple-combochart');
  assert.equal(result.edits[0].after.type, 'simple-table');
  assert.equal(result.edits[0].after.componentId, 'original');
});

const sourceBinding = [{ key: 'actionConceptList', value: [{
  name: 'qt_new', queryTimeTransformation: { dataTransformation: { sourceFieldName: 'numeric' } },
}] }];

test('dimension-filter uses its native controlField binding', async () => {
  const { result, disposed } = await run({ invalid: true, properties: [...sourceBinding, { key: 'metrics', value: [] }],
    targetType: 'dimension-filter' });
  assert.equal(result.shared_state_unchanged, true);
  assert.equal(result.edits[0].after.type, 'dimension-filter');
  assert.deepEqual(result.edits[0].after.behavior.onSelect, [{ action: 'lego-dimension-filter', isControl: true }]);
  assert.deepEqual(disposed, [2, 1]);
});

test('target fields are applied before validation of converted draft', async () => {
  const { result, disposed } = await run({ invalid: true, properties: sourceBinding });
  assert.equal(result.shared_state_unchanged, true);
  assert.equal(result.edits.length, 1);
  assert.deepEqual(disposed, [2, 1]);
});

test('validated calculated definitions do not require a physical source field', async () => {
  const formula = [{ key: 'actionConceptList', value: [{ name: 'qt_formula',
    queryTimeTransformation: { dataTransformation: {
      sourceFieldName: 'qt_formula', sourceType: 1, textFormula: 'ABS(t0.numeric)',
      frontendTextFormula: 'ABS(t0.numeric)', formulaOutputDataType: 2,
    } },
  }] }];
  const { result, disposed } = await run({ properties: formula, unknownField: true });
  assert.equal(result.shared_state_unchanged, true);
  assert.equal(result.edits.length, 1);
  assert.deepEqual(disposed, [2, 1]);
});

test('unknown or unapplied target fields reject conversion and dispose drafts', async () => {
  for (const [options, error] of [
    [{ unknownField: true }, 'UNKNOWN_FIELD'],
    [{ ignoreWrite: true }, 'PROPERTY_VALUE_NOT_APPLIED'],
    [{ enabled: false }, 'PROPERTY_DISABLED'],
  ]) {
    assert.deepEqual(await run({ properties: sourceBinding, ...options }), {
      result: { error }, disposed: [2, 1],
    });
  }
});
