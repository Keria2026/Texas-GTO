// Self-contained: this function is serialized into the selected CDP MAIN context.
// It inspects the shape of already-decoded dispatch arguments, never serializes their values.
export function installProbe(bindingName, controlName) {
  const root = globalThis;
  const version = 'wpk-dispatch-probe/0.1';
  const previous = Object.getOwnPropertyDescriptor(root, controlName)?.value;
  if (previous?.version === version) return { status: 'already_installed' };
  if (previous !== undefined) return { status: 'unsupported' };
  const binding = Object.getOwnPropertyDescriptor(root, bindingName)?.value;
  if (typeof binding !== 'function') return { status: 'binding_missing' };
  const notifications = new Set(['WP_actionNotify', 'WP_actionHistoryNotify', 'WP_userOptNotify',
    'WP_roundChangeNotify', 'C_playLogNotify', 'C_updateRoomDetail', 'C_closeRoomNotify']);
  let active = true, instance, original, wrapper, own, timer, lease, count = 0;
  let pending = 0, inspected = 0, limited = false;
  const seen = new Set();
  const emit = report => {
    if (!active || count >= 128) return;
    try { binding(JSON.stringify({ schema: 'tgto.wpk-probe/0.1', ...report })); count++; } catch { /* Page behavior is unaffected. */ }
  };
  const descriptor = (object, key) => {
    for (let depth = 0; object && depth < 5; depth++, object = Object.getPrototypeOf(object)) {
      const result = Object.getOwnPropertyDescriptor(object, key);
      if (result) return result;
    }
  };
  const value = (object, key) => descriptor(object, key)?.value;
  const stop = (reason = 'stopped') => {
    if (!active) return;
    emit({ type: 'state', state: reason });
    active = false; clearInterval(timer); clearTimeout(lease);
    try {
      if (instance && Object.getOwnPropertyDescriptor(instance, '_dispatchMessageToCC')?.value === wrapper) {
        if (own) Object.defineProperty(instance, '_dispatchMessageToCC', own);
        else delete instance._dispatchMessageToCC;
      }
      if (Object.getOwnPropertyDescriptor(root, controlName)?.value === control) delete root[controlName];
      if (Object.getOwnPropertyDescriptor(root, bindingName)?.value === binding) delete root[bindingName];
    } catch { /* A later page wrapper owns its method; never overwrite it. */ }
  };
  const control = { version, stop };
  Object.defineProperty(root, controlName, { value: control, configurable: true });
  const inspect = (args, threw) => {
    if (!active) return;
    try {
      const fields = [], visited = new Set(); let truncated = false, message = null;
      const add = (path, type) => {
        if (fields.length >= 64) { truncated = true; return; }
        if (!fields.some(f => f.path === path && f.type === type)) fields.push({ path, type });
      };
      const walk = (input, path, depth) => {
        if (fields.length >= 64) { truncated = true; return; }
        const kind = input === null ? 'null' : typeof input;
        if (kind === 'string' && notifications.has(input)) message ??= input;
        if (kind !== 'object') { add(path, kind); return; }
        if (visited.has(input)) { add(path, 'circular'); return; }
        visited.add(input);
        if (depth > 3) { add(path, 'depth_limit'); truncated = true; return; }
        if (Array.isArray(input)) {
          add(path, 'array');
          // One representative item's shape only. Never enumerate card arrays or dictionary values.
          const item = Object.getOwnPropertyDescriptor(input, '0');
          if (item && 'value' in item) walk(item.value, `${path}[]`, depth + 1);
          return;
        }
        add(path, 'object');
        const keys = Object.getOwnPropertyNames(input);
        if (keys.length > 24) truncated = true;
        for (const key of keys.slice(0, 24)) {
          const safe = /^[A-Za-z_$][A-Za-z0-9_$]{0,39}$/.test(key) ? key : '*';
          const next = `${path}.${safe}`;
          // Do not descend into identifiers, private cards, credentials or dynamic maps.
          if (safe === '*' || /token|cookie|auth|pass|secret|key|session|account|nickname|user.?id|uid|phone|email|ip.?addr|device|card|poker|hole/i.test(key)) {
            add(next, 'redacted'); continue;
          }
          const field = Object.getOwnPropertyDescriptor(input, key);
          if (!field || !('value' in field)) { add(next, 'accessor'); continue; }
          walk(field.value, next, depth + 1);
        }
      };
      for (let i = 0; i < Math.min(args.length, 4); i++) walk(args[i], `arg${i}`, 0);
      if (args.length > 4) truncated = true;
      const report = { type: 'shape', message, phase: 'after_dispatch_microtask', threw, fields, truncated };
      const key = JSON.stringify(report);
      if (!seen.has(key)) {
        if (seen.size >= 64) { stop('sample_limit'); return; }
        seen.add(key); emit(report);
      }
    } catch { emit({ type: 'state', state: 'inspection_failed' }); }
  };
  const attempt = () => {
    if (!active || instance) return;
    try {
      let module = value(root, 'HMFClient'), entry = 'global';
      if (!module) {
        const require = value(root, '__require');
        if (typeof require === 'function') {
          try { module = Reflect.apply(require, root, ['HMFClient']); entry = 'cocos_module'; } catch { return; }
        }
      }
      const Client = module && (value(module, 'default') ?? value(module, 'HMFClient') ?? module);
      const shared = Client && value(Client, 'sharedInstance');
      if (typeof shared !== 'function') return;
      const candidate = Reflect.apply(shared, Client, []);
      if (!candidate) return;
      const method = descriptor(candidate, '_dispatchMessageToCC');
      if (!method || typeof method.value !== 'function' || method.writable === false) { stop('entry_unsupported'); return; }
      own = Object.getOwnPropertyDescriptor(candidate, '_dispatchMessageToCC');
      original = method.value;
      wrapper = function (...args) {
        let threw = true;
        try { const result = Reflect.apply(original, this, args); threw = false; return result; }
        finally {
          // Preserve return values (including Promise identity), thrown errors and call count.
          try {
            if (active && !limited) {
              if (pending >= 8 || inspected >= 256) {
                limited = true;
                if (pending === 0) stop('sample_limit');
              } else {
                pending++; inspected++;
                queueMicrotask(() => {
                  try { inspect(args, threw); }
                  finally { pending--; if (limited && pending === 0) stop('sample_limit'); }
                });
              }
            }
          } catch { /* Never replace page results. */ }
        }
      };
      Object.defineProperty(candidate, '_dispatchMessageToCC', own ? { ...own, value: wrapper }
        : { value: wrapper, configurable: true, writable: true });
      instance = candidate;
      emit({ type: 'state', state: 'observing', entry });
    } catch { stop('entry_unsupported'); }
  };
  emit({ type: 'state', state: 'probing' });
  timer = setInterval(() => {
    if (instance) {
      try { if (value(instance, '_dispatchMessageToCC') !== wrapper) stop('entry_replaced'); }
      catch { stop('entry_unsupported'); }
    } else attempt();
  }, 500);
  // Finite lease also restores the page when the extension worker disappears unexpectedly.
  lease = setTimeout(() => stop(instance ? 'expired' : 'entry_missing'), 120000);
  attempt();
  return { status: 'installed' };
}
