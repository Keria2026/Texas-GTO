import vm from 'node:vm';
import { readFile } from 'node:fs/promises';
import { installWpkPage } from '../extension/wpk-page.ts';
import { wpkCall } from '../extension/core.ts';
import { core } from './helpers.ts';
const clientSource = await readFile(new URL('./fixtures/wpk-client.js', import.meta.url), 'utf8');
export function fixture(unknownCards = false) {
  const messages: any[] = [], timers: (() => void)[] = [];
  const scope: any = { setInterval: (fn: () => void) => { timers.push(fn); return timers.length; }, clearInterval() {},
    emitBinding: (text: string) => messages.push(JSON.parse(text)) };
  vm.runInNewContext(clientSource, scope);
  if (unknownCards) scope.__wpkFixture.game.sitUserList[0].handCards = [-1, -1];
  vm.runInNewContext(`(${installWpkPage.toString()})('emitBinding')`, scope);
  return { scope, messages, timers, fixture: scope.__wpkFixture };
}
export function normalize(messages: any[]) {
  let checkpoint: any = null; const events: any[] = [];
  for (const { dto } of messages) if (dto) { const r = wpkCall(core, { op: 'reduce', message: dto, checkpoint }); checkpoint = r.checkpoint; events.push(...r.events); }
  return { checkpoint, events };
}
