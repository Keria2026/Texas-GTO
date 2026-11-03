//! WPK DTO normalization and deterministic observation reducer. No browser or account access.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

type Result<T> = std::result::Result<T, String>;
const SAFE: i64 = 9_007_199_254_740_991;
const ACTIONS: &[&str] = &[
    "PAY_MUSHROOM",
    "SMALL_BLIND",
    "BIG_BLIND",
    "THIRD_BLIND",
    "ANTE",
    "BET",
    "RAISE",
    "CALL",
    "ALL_IN",
    "FOLD",
    "CHECK",
];

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Player {
    pub player_id: String,
    pub seat: u8,
    pub stack: Option<i64>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Action {
    pub source_event_id: String,
    pub player_id: String,
    pub action: String,
    pub amount_delta: i64,
    pub amount_to: Option<i64>,
    pub source_round: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Turn {
    pub player_id: String,
    pub legal_actions: Vec<String>,
    pub call_amount: Option<i64>,
    pub min_raise_reported: Option<i64>,
    pub max_raise_reported: Option<i64>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResultPlayer {
    pub player_id: String,
    pub net_chips: Option<i64>,
    pub mushroom_paid: Option<i64>,
    pub mushroom_awarded: Option<i64>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ResultInput {
    pub table_id: String,
    pub hand_number: u64,
    pub players: Vec<ResultPlayer>,
    pub double_board: bool,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LogInput {
    pub table_id: String,
    pub hand_number: u64,
    pub players: Vec<ResultPlayer>,
    pub board: Vec<i64>,
    pub hero_cards: Vec<Option<i64>>,
    pub double_board: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Visible {
    pub table_id: String,
    pub hand_number: u64,
    pub round: String,
    pub hero_cards: Vec<Option<i64>>,
    pub board: Vec<i64>,
    pub players: Vec<Player>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Message {
    pub version: String,
    pub event: String,
    pub visible: Visible,
    pub deal_hand: Option<u64>,
    pub actions: Vec<Action>,
    pub turn: Option<Turn>,
    pub result: Option<ResultInput>,
    pub log: Option<LogInput>,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Settlement {
    pub table_id: String,
    pub hand_number: u64,
    pub players: Vec<ResultPlayer>,
    pub board: Vec<String>,
    pub hero_cards: Vec<Option<String>>,
    pub status: String,
    pub reason: Option<String>,
    pub pool_change: Option<i64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ObservedHand {
    pub table_id: String,
    pub hand_number: u64,
    pub hero_cards: Vec<Option<String>>,
    pub board: Vec<String>,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct State {
    pub table_id: Option<String>,
    pub hand_number: Option<u64>,
    pub street: String,
    pub hero_cards: Vec<Option<String>>,
    pub board: Vec<String>,
    pub players: Vec<Player>,
    pub actions: Vec<Action>,
    pub pending_actions: Vec<Action>,
    pub turn: Option<Turn>,
    pub pending: bool,
    pub saw_start: bool,
    pub seen_deal: bool,
    pub history_complete: bool,
    pub closed: bool,
    pub results: Vec<ResultInput>,
    pub logs: Vec<LogInput>,
    pub settlements: Vec<Settlement>,
    pub observed: Vec<ObservedHand>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Event {
    pub kind: String,
    pub table_id: Option<String>,
    pub hand_id: Option<String>,
    pub data: Value,
}
#[derive(Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
enum Request {
    #[serde(rename = "reduce")]
    Reduce {
        message: Box<Message>,
        checkpoint: Option<Box<State>>,
    },
    #[serde(rename = "validate")]
    Validate { record: Box<Record> },
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    schema: String,
    r#type: String,
    source_id: String,
    sequence: String,
    captured_at: String,
    adapter_version: String,
    kind: String,
    table_id: Option<String>,
    hand_id: Option<String>,
    data: Value,
}
fn ensure(ok: bool, text: &str) -> Result<()> {
    if ok { Ok(()) } else { Err(text.into()) }
}
fn alias(id: &str) -> bool {
    id == "hero" || id.strip_prefix("player-").is_some_and(decimal)
}
fn table(id: &str) -> bool {
    id.strip_prefix("table-").is_some_and(decimal)
}
fn decimal(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 20
        && s.bytes().all(|b| b.is_ascii_digit())
        && !s.starts_with('0')
        && s.parse::<u64>().is_ok()
}
fn chips(n: i64, signed: bool) -> bool {
    (-SAFE..=SAFE).contains(&n) && (signed || n >= 0)
}
fn optional_chips(n: Option<i64>, signed: bool) -> bool {
    n.is_none_or(|v| chips(v, signed))
}
fn hand(n: u64) -> bool {
    n > 0 && n <= SAFE as u64
}
fn players(list: &[Player]) -> Result<()> {
    ensure(list.len() <= 8, "Too many players")?;
    for (i, p) in list.iter().enumerate() {
        ensure(
            alias(&p.player_id) && (1..=8).contains(&p.seat) && optional_chips(p.stack, false),
            "Invalid player",
        )?;
        ensure(
            !list[..i]
                .iter()
                .any(|q| q.player_id == p.player_id || q.seat == p.seat),
            "Duplicate player/seat",
        )?;
    }
    Ok(())
}
fn action(a: &Action) -> Result<()> {
    ensure(
        decimal(&a.source_event_id) && alias(&a.player_id) && ACTIONS.contains(&a.action.as_str()),
        "Invalid action identity/type",
    )?;
    ensure(
        chips(a.amount_delta, false) && optional_chips(a.amount_to, false),
        "Invalid action amount",
    )?;
    ensure(
        !["FOLD", "CHECK"].contains(&a.action.as_str()) || a.amount_delta == 0,
        "Fold/check cannot debit chips",
    )?;
    ensure(
        a.source_round.as_ref().is_none_or(|r| r.len() <= 32),
        "Invalid source round",
    )
}
fn turn(t: &Turn) -> Result<()> {
    ensure(
        alias(&t.player_id)
            && t.legal_actions.len() <= 6
            && t.legal_actions
                .iter()
                .all(|a| ["FOLD", "CHECK", "CALL", "BET", "RAISE", "ALL_IN"].contains(&a.as_str())),
        "Invalid turn",
    )?;
    ensure(
        t.player_id == "hero"
            || (t.legal_actions.is_empty()
                && t.call_amount.is_none()
                && t.min_raise_reported.is_none()
                && t.max_raise_reported.is_none()),
        "Opponent private options forbidden",
    )?;
    ensure(
        [t.call_amount, t.min_raise_reported, t.max_raise_reported]
            .iter()
            .all(|v| optional_chips(*v, false)),
        "Invalid turn amount",
    )
}
fn result_players(list: &[ResultPlayer]) -> Result<()> {
    ensure(
        !list.is_empty() && list.len() <= 8,
        "Invalid settlement player count",
    )?;
    for (i, p) in list.iter().enumerate() {
        ensure(
            alias(&p.player_id)
                && optional_chips(p.net_chips, true)
                && optional_chips(p.mushroom_paid, false)
                && optional_chips(p.mushroom_awarded, false),
            "Invalid settlement player",
        )?;
        ensure(
            !list[..i].iter().any(|q| q.player_id == p.player_id),
            "Duplicate settlement player",
        )?;
    }
    Ok(())
}
fn compare_final(players: &[ResultPlayer], old: &Settlement) -> Result<()> {
    ensure(
        players.len() == old.players.len(),
        "Final settlement player count changed",
    )?;
    for p in players {
        let q = old
            .players
            .iter()
            .find(|q| q.player_id == p.player_id)
            .ok_or("Final settlement player missing")?;
        for (a, b) in [
            (p.net_chips, q.net_chips),
            (p.mushroom_paid, q.mushroom_paid),
            (p.mushroom_awarded, q.mushroom_awarded),
        ] {
            if a.is_some() {
                ensure(a == b, "Conflicting or enriched immutable final settlement")?;
            }
        }
    }
    Ok(())
}
pub fn card(code: Option<i64>) -> Result<Option<(String, u8)>> {
    let Some(code) = code else { return Ok(None) };
    if code == 0 || code == -1 {
        return Ok(None);
    }
    let (suit, rank) = (code / 100, code % 100);
    ensure(
        (1..=4).contains(&suit) && (1..=13).contains(&rank),
        "Invalid WPK card",
    )?;
    let s = ["s", "h", "c", "d"][(suit - 1) as usize];
    let r = [
        "A", "2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K",
    ][(rank - 1) as usize];
    let index = (if rank == 1 { 12 } else { rank - 2 }) * 4 + [3, 2, 0, 1][(suit - 1) as usize];
    Ok(Some((format!("{r}{s}"), index as u8)))
}
fn cards(hero: &[Option<i64>], board: &[i64]) -> Result<(Vec<Option<String>>, Vec<String>)> {
    ensure(
        matches!(hero.len(), 0 | 2) && matches!(board.len(), 0 | 3 | 4 | 5),
        "Invalid card count",
    )?;
    let h: Vec<_> = if hero.is_empty() {
        vec![None, None]
    } else {
        hero.iter()
            .map(|c| card(*c).map(|p| p.map(|x| x.0)))
            .collect::<Result<_>>()?
    };
    let b: Vec<_> = board
        .iter()
        .map(|c| {
            card(Some(*c))?
                .map(|p| p.0)
                .ok_or_else(|| "Unknown public card".into())
        })
        .collect::<Result<_>>()?;
    unique(&h, &b)?;
    Ok((h, b))
}
fn unique(hero: &[Option<String>], board: &[String]) -> Result<()> {
    let mut seen = std::collections::BTreeSet::new();
    for c in hero.iter().flatten().chain(board) {
        ensure(seen.insert(c), "Duplicate cards")?;
    }
    Ok(())
}
fn street(round: &str) -> &'static str {
    match round {
        "BET_BLIND" | "PRE_FLOP" => "preflop",
        "FLOP" => "flop",
        "TURN" => "turn",
        "RIVER" => "river",
        "THAN" => "showdown",
        "CLEAN" => "between_hands",
        _ => "unknown",
    }
}
fn scope(table: &Option<String>, number: Option<u64>) -> Option<String> {
    table.as_ref().zip(number).map(|(t, n)| format!("{t}:{n}"))
}
fn event(events: &mut Vec<Event>, s: &State, kind: &str, data: Value) {
    events.push(Event {
        kind: kind.into(),
        table_id: s.table_id.clone(),
        hand_id: scope(&s.table_id, s.hand_number),
        data,
    });
}
fn view(s: &State) -> Value {
    json!({"street":s.street,"hero_cards":s.hero_cards,"board":s.board,"players":s.players,"turn":s.turn,"history_complete":s.history_complete,"rules_version":"wpk-observed-unverified-v1"})
}
fn add_action(list: &mut Vec<Action>, mut a: Action) -> Result<Option<(String, Action)>> {
    action(&a)?;
    if ["PAY_MUSHROOM", "FOLD", "CHECK"].contains(&a.action.as_str()) {
        a.amount_to = None;
    }
    if let Some(old) = list
        .iter_mut()
        .find(|q| q.source_event_id == a.source_event_id)
    {
        ensure(
            old.player_id == a.player_id
                && old.action == a.action
                && old.amount_delta == a.amount_delta
                && (old.amount_to.is_none()
                    || a.amount_to.is_none()
                    || old.amount_to == a.amount_to)
                && (old.source_round.is_none()
                    || a.source_round.is_none()
                    || old.source_round == a.source_round),
            "Conflicting action history",
        )?;
        let previous = old.clone();
        if old.amount_to.is_none() {
            old.amount_to = a.amount_to;
        }
        if old.source_round.is_none() {
            old.source_round = a.source_round;
        }
        return Ok((previous != *old).then(|| ("action_updated".into(), old.clone())));
    }
    ensure(list.len() < 256, "Action history limit")?;
    list.push(a.clone());
    list.sort_by_key(|a| a.source_event_id.parse::<u64>().unwrap_or(0));
    Ok(Some(("action_confirmed".into(), a)))
}
fn reconcile(s: &mut State, events: &mut Vec<Event>) -> Result<()> {
    for log in &s.logs {
        let Some(result) = s
            .results
            .iter()
            .find(|r| r.table_id == log.table_id && r.hand_number == log.hand_number)
        else {
            continue;
        };
        ensure(
            !log.double_board && !result.double_board,
            "Double-board rules unsupported",
        )?;
        result_players(&log.players)?;
        result_players(&result.players)?;
        ensure(
            log.players.len() == result.players.len(),
            "Settlement players disagree",
        )?;
        let (mut hero, board) = cards(&log.hero_cards, &log.board)?;
        if let Some(observed) = s
            .observed
            .iter()
            .find(|h| h.table_id == log.table_id && h.hand_number == log.hand_number)
        {
            if !observed.board.is_empty() {
                ensure(observed.board == board, "Settlement public cards disagree")?;
            }
            for (a, b) in observed.hero_cards.iter().zip(&hero) {
                if a.is_some() && b.is_some() {
                    ensure(a == b, "Settlement own cards disagree")?;
                }
            }
            unique(&observed.hero_cards, &board)?;
            for (a, b) in hero.iter_mut().zip(&observed.hero_cards) {
                if a.is_none() {
                    *a = b.clone();
                }
            }
        }
        let mut merged = Vec::new();
        for p in &log.players {
            let q = result
                .players
                .iter()
                .find(|q| q.player_id == p.player_id)
                .ok_or("Settlement player missing")?;
            for (a, b) in [
                (p.net_chips, q.net_chips),
                (p.mushroom_paid, q.mushroom_paid),
                (p.mushroom_awarded, q.mushroom_awarded),
            ] {
                if a.is_some() && b.is_some() {
                    ensure(a == b, "Settlement amounts disagree")?;
                }
            }
            merged.push(ResultPlayer {
                player_id: p.player_id.clone(),
                net_chips: p.net_chips.or(q.net_chips),
                mushroom_paid: p.mushroom_paid.or(q.mushroom_paid),
                mushroom_awarded: p.mushroom_awarded.or(q.mushroom_awarded),
            });
        }
        let complete = merged.iter().all(|p| {
            p.net_chips.is_some() && p.mushroom_paid.is_some() && p.mushroom_awarded.is_some()
        });
        let pool = complete.then(|| {
            merged
                .iter()
                .map(|p| {
                    i128::from(p.mushroom_paid.unwrap_or(0))
                        - i128::from(p.mushroom_awarded.unwrap_or(0))
                })
                .sum::<i128>()
        });
        let total = merged
            .iter()
            .map(|p| i128::from(p.net_chips.unwrap_or(0)))
            .sum::<i128>();
        let balanced =
            complete && pool.is_some_and(|p| p + total == 0 && p.abs() <= i128::from(SAFE));
        let settlement = Settlement {
            table_id: log.table_id.clone(),
            hand_number: log.hand_number,
            players: merged,
            board,
            hero_cards: hero,
            status: if balanced { "reconciled" } else { "unresolved" }.into(),
            reason: (!balanced).then(|| {
                if complete {
                    "point_difference"
                } else {
                    "missing_amounts"
                }
                .into()
            }),
            pool_change: pool
                .filter(|p| p.abs() <= i128::from(SAFE))
                .map(|p| p as i64),
        };
        if let Some(old) = s
            .settlements
            .iter()
            .find(|v| v.table_id == log.table_id && v.hand_number == log.hand_number)
        {
            ensure(old == &settlement, "Conflicting final settlement")?;
            continue;
        }
        events.push(Event {
            kind: "settlement".into(),
            table_id: Some(log.table_id.clone()),
            hand_id: Some(format!("{}:{}", log.table_id, log.hand_number)),
            data: serde_json::to_value(&settlement).map_err(|_| "Serialize settlement")?,
        });
        s.settlements.push(settlement);
    }
    // Completed settlements are already archived as immutable events; retain eight in the checkpoint.
    while s.settlements.len() > 8 {
        s.settlements.remove(0);
    }
    s.results.retain(|r| {
        !s.settlements
            .iter()
            .any(|v| v.table_id == r.table_id && v.hand_number == r.hand_number)
    });
    s.logs.retain(|r| {
        !s.settlements
            .iter()
            .any(|v| v.table_id == r.table_id && v.hand_number == r.hand_number)
    });
    Ok(())
}
fn checkpoint_valid(s: &State) -> Result<()> {
    ensure(
        s.table_id.is_some() == s.hand_number.is_some(),
        "Invalid checkpoint scope",
    )?;
    ensure(
        s.table_id.as_ref().is_none_or(|t| table(t)) && s.hand_number.is_none_or(hand),
        "Invalid checkpoint table/hand",
    )?;
    ensure(
        [
            "",
            "unknown",
            "preflop",
            "flop",
            "turn",
            "river",
            "showdown",
            "between_hands",
        ]
        .contains(&s.street.as_str()),
        "Invalid checkpoint street",
    )?;
    ensure(
        matches!(s.hero_cards.len(), 0 | 2) && matches!(s.board.len(), 0 | 3 | 4 | 5),
        "Invalid checkpoint cards",
    )?;
    validate_text_cards(&s.hero_cards)?;
    validate_text_cards(&s.board.iter().cloned().map(Some).collect::<Vec<_>>())?;
    unique(&s.hero_cards, &s.board)?;
    players(&s.players)?;
    for a in s.actions.iter().chain(&s.pending_actions) {
        action(a)?;
    }
    if let Some(t) = &s.turn {
        turn(t)?;
    }
    for r in &s.results {
        ensure(
            table(&r.table_id) && hand(r.hand_number) && !r.double_board,
            "Invalid checkpoint result",
        )?;
        result_players(&r.players)?;
    }
    for l in &s.logs {
        ensure(
            table(&l.table_id) && hand(l.hand_number) && !l.double_board,
            "Invalid checkpoint log",
        )?;
        result_players(&l.players)?;
        cards(&l.hero_cards, &l.board)?;
    }
    for h in &s.observed {
        ensure(
            table(&h.table_id) && hand(h.hand_number),
            "Invalid observed hand",
        )?;
        validate_text_cards(&h.hero_cards)?;
        validate_text_cards(&h.board.iter().cloned().map(Some).collect::<Vec<_>>())?;
        unique(&h.hero_cards, &h.board)?;
    }
    for settled in &s.settlements {
        result_players(&settled.players)?;
        ensure(
            table(&settled.table_id) && hand(settled.hand_number),
            "Invalid checkpoint settlement",
        )?;
    }
    Ok(())
}
pub fn reduce(m: Message, mut s: State) -> Result<(Vec<Event>, State)> {
    checkpoint_valid(&s)?;
    ensure(m.version == "wpk/1", "Unknown adapter version")?;
    ensure(
        [
            "snapshot",
            "WP_bankerChangeNotify",
            "WP_dealNotify",
            "WP_actionNotify",
            "WP_actionHistoryNotify",
            "WP_userOptNotify",
            "WP_roundChangeNotify",
            "WP_playResultNotify",
            "C_playLogNotify",
            "C_cleanNotify",
            "C_closeRoomNotify",
        ]
        .contains(&m.event.as_str()),
        "Unknown WPK message",
    )?;
    let v = &m.visible;
    ensure(
        table(&v.table_id) && hand(v.hand_number),
        "Invalid table/hand",
    )?;
    players(&v.players)?;
    ensure(
        v.players.iter().any(|p| p.player_id == "hero"),
        "Own visible seat missing",
    )?;
    ensure(
        m.actions.len() <= 256
            && s.actions.len() <= 256
            && s.pending_actions.len() <= 256
            && s.results.len() <= 8
            && s.logs.len() <= 8
            && s.observed.len() <= 8
            && s.settlements.len() <= 8,
        "Checkpoint/message limit",
    )?;
    let mut events = Vec::new();
    let first = s.table_id.is_none();
    let previous_view = view(&s);
    let old_turn = s.turn.clone();
    let number = m.deal_hand.unwrap_or(v.hand_number);
    ensure(hand(number), "Invalid deal hand")?;
    let changed = s.table_id.as_ref() != Some(&v.table_id) || s.hand_number != Some(number);
    let stale =
        s.table_id.as_ref() == Some(&v.table_id) && s.hand_number.is_some_and(|n| number < n);
    if changed && !stale {
        let same_table = s.table_id.as_ref() == Some(&v.table_id);
        s.table_id = Some(v.table_id.clone());
        s.hand_number = Some(number);
        s.hero_cards = vec![None, None];
        s.board.clear();
        s.turn = None;
        s.closed = false;
        s.actions = if same_table && s.pending {
            std::mem::take(&mut s.pending_actions)
        } else {
            Vec::new()
        };
        s.history_complete = same_table && s.saw_start && m.event == "WP_dealNotify";
        s.pending = false;
        s.saw_start = false;
        s.seen_deal = false;
    }
    if m.event == "WP_bankerChangeNotify" {
        s.pending = true;
        s.saw_start = true;
        s.pending_actions.clear();
        s.turn = None;
    }
    if m.event == "WP_dealNotify" && !stale && !s.seen_deal {
        if !changed {
            s.actions = if s.pending {
                std::mem::take(&mut s.pending_actions)
            } else {
                s.actions
            };
            s.history_complete = s.saw_start;
            s.pending = false;
            s.saw_start = false;
        }
        event(
            &mut events,
            &s,
            "hand_started",
            json!({"hand_number":number,"history_complete":s.history_complete}),
        );
        for a in &s.actions {
            event(
                &mut events,
                &s,
                "action_confirmed",
                serde_json::to_value(a).map_err(|_| "Serialize pending action")?,
            );
        }
        s.seen_deal = true;
    }
    if !stale {
        let between = s.pending || v.round == "CLEAN";
        let (mut hero, board) = if between || v.hand_number != number {
            (vec![None, None], vec![])
        } else {
            cards(&v.hero_cards, &v.board)?
        };
        if !between && !changed {
            for (old, new) in s.hero_cards.iter().zip(&mut hero) {
                if let Some(known) = old {
                    if let Some(now) = new {
                        ensure(now == known, "Own hand cards conflicted")?;
                    } else {
                        *new = Some(known.clone());
                    }
                }
            }
        }
        if s.hero_cards != hero {
            s.hero_cards = hero;
            event(&mut events, &s, "hero_cards", json!({"cards":s.hero_cards}));
        }
        if s.board != board {
            ensure(
                s.board.is_empty() || board.starts_with(&s.board) || between,
                "Public board regressed/conflicted",
            )?;
            s.board = board;
            event(&mut events, &s, "board_updated", json!({"cards":s.board}));
        }
        let next_street = if between {
            "between_hands".into()
        } else {
            street(&v.round).into()
        };
        if s.street != next_street {
            s.turn = None;
        }
        s.street = next_street;
        if m.event == "WP_playResultNotify" {
            s.street = "showdown".into();
        }
        if s.players != v.players {
            s.players = v.players.clone();
            event(
                &mut events,
                &s,
                "players_updated",
                json!({"players":s.players}),
            );
        }
        for a in m.actions {
            if s.turn.as_ref().is_some_and(|t| t.player_id == a.player_id)
                && m.event == "WP_actionNotify"
            {
                s.turn = None;
            }
            if s.pending {
                add_action(&mut s.pending_actions, a)?;
            } else if let Some((kind, action)) = add_action(&mut s.actions, a)? {
                event(
                    &mut events,
                    &s,
                    &kind,
                    serde_json::to_value(action).map_err(|_| "Serialize action")?,
                );
            }
        }
        if ["WP_userOptNotify", "WP_roundChangeNotify"].contains(&m.event.as_str()) {
            if let Some(t) = &m.turn {
                turn(t)?;
            }
            s.turn = m.turn;
        }
        if !["preflop", "flop", "turn", "river"].contains(&s.street.as_str())
            || ["WP_playResultNotify", "C_cleanNotify", "C_closeRoomNotify"]
                .contains(&m.event.as_str())
        {
            s.turn = None;
        }
        if old_turn != s.turn {
            event(&mut events, &s, "action_requested", json!({"turn":s.turn}));
        }
        if s.actions.windows(2).any(|w| {
            w[1].source_event_id.parse::<u64>().unwrap_or(0)
                != w[0]
                    .source_event_id
                    .parse::<u64>()
                    .unwrap_or(0)
                    .saturating_add(1)
        }) {
            s.history_complete = false;
        }
        if !between {
            let h = ObservedHand {
                table_id: v.table_id.clone(),
                hand_number: number,
                hero_cards: s.hero_cards.clone(),
                board: s.board.clone(),
            };
            if let Some(old) = s
                .observed
                .iter_mut()
                .find(|h| h.table_id == v.table_id && h.hand_number == number)
            {
                *old = h;
            } else {
                s.observed.push(h);
            }
            while s.observed.len() > 8 {
                s.observed.remove(0);
            }
        }
    }
    if let Some(mut r) = m.result {
        ensure(
            table(&r.table_id) && hand(r.hand_number) && !r.double_board,
            "Invalid/unsupported result scope",
        )?;
        result_players(&r.players)?;
        r.players.sort_by(|a, b| a.player_id.cmp(&b.player_id));
        if let Some(old) = s
            .results
            .iter()
            .find(|p| p.table_id == r.table_id && p.hand_number == r.hand_number)
        {
            ensure(old == &r, "Conflicting result")?;
        } else if let Some(old) = s
            .settlements
            .iter()
            .find(|p| p.table_id == r.table_id && p.hand_number == r.hand_number)
        {
            compare_final(&r.players, old)?;
        } else {
            events.push(Event {
                kind: "settlement_pending".into(),
                table_id: Some(r.table_id.clone()),
                hand_id: Some(format!("{}:{}", r.table_id, r.hand_number)),
                data: json!({"received":"result","players":r.players}),
            });
            s.results.push(r);
        }
    }
    if let Some(mut l) = m.log {
        ensure(
            table(&l.table_id) && hand(l.hand_number) && !l.double_board,
            "Invalid/unsupported final log",
        )?;
        result_players(&l.players)?;
        cards(&l.hero_cards, &l.board)?;
        l.players.sort_by(|a, b| a.player_id.cmp(&b.player_id));
        if let Some(old) = s
            .logs
            .iter()
            .find(|p| p.table_id == l.table_id && p.hand_number == l.hand_number)
        {
            ensure(old == &l, "Conflicting final log")?;
        } else if let Some(old) = s
            .settlements
            .iter()
            .find(|p| p.table_id == l.table_id && p.hand_number == l.hand_number)
        {
            compare_final(&l.players, old)?;
            ensure(
                cards(&l.hero_cards, &l.board)?.1 == old.board,
                "Conflicting final board",
            )?;
        } else {
            events.push(Event {
                kind: "settlement_pending".into(),
                table_id: Some(l.table_id.clone()),
                hand_id: Some(format!("{}:{}", l.table_id, l.hand_number)),
                data: json!({"received":"log","players":l.players}),
            });
            s.logs.push(l);
        }
    }
    ensure(
        s.results.len() <= 8 && s.logs.len() <= 8,
        "Unmatched settlement limit",
    )?;
    reconcile(&mut s, &mut events)?;
    if m.event == "C_closeRoomNotify" && !s.closed {
        s.closed = true;
        s.turn = None;
        s.history_complete = false;
        event(&mut events, &s, "room_closed", json!({"reconciled":false}));
    }
    // Street/history transitions and unchanged players on a new hand must be
    // transmitted too: the receiver cannot reconstruct them from card deltas.
    if first || (changed && !stale) || previous_view != view(&s) {
        event(&mut events, &s, "snapshot", view(&s));
    }
    Ok((events, s))
}

fn validate(record: Record) -> Result<()> {
    ensure(
        record.kind == "capture_gap" || (record.table_id.is_some() && record.hand_id.is_some()),
        "Business event scope missing",
    )?;
    if let Some(id) = &record.hand_id {
        let (t, n) = id.split_once(':').ok_or("Invalid hand scope")?;
        ensure(
            table(t) && decimal(n) && record.table_id.as_deref() == Some(t),
            "Business table/hand mismatch",
        )?;
    }
    ensure(
        record.schema == "tgto.capture/0.1"
            && record.r#type == "business_event"
            && record.adapter_version == "wpk/1",
        "Invalid business envelope",
    )?;
    ensure(
        !record.source_id.is_empty()
            && record.source_id.len() <= 80
            && decimal(&record.sequence)
            && record.captured_at.len() <= 40,
        "Invalid business identity",
    )?;
    ensure(
        record.table_id.as_ref().is_none_or(|t| table(t))
            && record.hand_id.as_ref().is_none_or(|h| h.len() <= 64),
        "Invalid business scope",
    )?;
    // Round-trip each typed data shape; unknown fields, private options and invalid amounts fail closed.
    match record.kind.as_str() {
        "action_confirmed" | "action_updated" => action(
            &serde_json::from_value::<Action>(record.data).map_err(|_| "Invalid action data")?,
        ),
        "settlement" => {
            let s: Settlement =
                serde_json::from_value(record.data).map_err(|_| "Invalid settlement data")?;
            result_players(&s.players)?;
            ensure(
                record.table_id.as_deref() == Some(s.table_id.as_str())
                    && record.hand_id == Some(format!("{}:{}", s.table_id, s.hand_number)),
                "Settlement envelope scope mismatch",
            )?;
            ensure(
                optional_chips(s.pool_change, true),
                "Invalid settlement pool amount",
            )?;
            ensure(
                table(&s.table_id) && hand(s.hand_number) && matches!(s.board.len(), 0 | 3 | 4 | 5),
                "Invalid settlement scope/board",
            )?;
            validate_text_cards(&s.board.iter().cloned().map(Some).collect::<Vec<_>>())?;
            ensure(s.hero_cards.len() == 2, "Invalid settlement hand cards")?;
            validate_text_cards(&s.hero_cards)?;
            unique(&s.hero_cards, &s.board)?;
            if s.status == "reconciled" {
                ensure(
                    s.reason.is_none()
                        && s.players.iter().all(|p| {
                            p.net_chips.is_some()
                                && p.mushroom_paid.is_some()
                                && p.mushroom_awarded.is_some()
                        }),
                    "Incomplete reconciled settlement",
                )?;
                let pool = s
                    .players
                    .iter()
                    .map(|p| {
                        i128::from(p.mushroom_paid.unwrap_or(0))
                            - i128::from(p.mushroom_awarded.unwrap_or(0))
                    })
                    .sum::<i128>();
                let net = s
                    .players
                    .iter()
                    .map(|p| i128::from(p.net_chips.unwrap_or(0)))
                    .sum::<i128>();
                ensure(
                    pool + net == 0 && s.pool_change.map(i128::from) == Some(pool),
                    "Settlement conservation failed",
                )
            } else {
                ensure(
                    s.status == "unresolved"
                        && [Some("point_difference"), Some("missing_amounts")]
                            .contains(&s.reason.as_deref()),
                    "Invalid settlement status",
                )
            }
        }
        "settlement_pending" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Pending {
                received: String,
                players: Vec<ResultPlayer>,
            }
            let p: Pending =
                serde_json::from_value(record.data).map_err(|_| "Invalid pending settlement")?;
            ensure(
                ["result", "log"].contains(&p.received.as_str()),
                "Invalid pending evidence",
            )?;
            result_players(&p.players)
        }
        "capture_gap" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Gap {
                reason: String,
            }
            let g: Gap = serde_json::from_value(record.data).map_err(|_| "Invalid gap")?;
            ensure(g.reason.len() <= 512, "Gap too long")
        }
        "hero_cards" | "board_updated" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Cards {
                cards: Vec<Option<String>>,
            }
            let c: Cards = serde_json::from_value(record.data).map_err(|_| "Invalid cards")?;
            ensure(
                if record.kind == "hero_cards" {
                    c.cards.len() == 2
                } else {
                    matches!(c.cards.len(), 0 | 3 | 4 | 5) && c.cards.iter().all(Option::is_some)
                },
                "Invalid card count/presence",
            )?;
            validate_text_cards(&c.cards)
        }
        "players_updated" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct P {
                players: Vec<Player>,
            }
            let p: P = serde_json::from_value(record.data).map_err(|_| "Invalid players")?;
            players(&p.players)
        }
        "action_requested" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct T {
                turn: Option<Turn>,
            }
            let t: T = serde_json::from_value(record.data).map_err(|_| "Invalid turn")?;
            t.turn.as_ref().map_or(Ok(()), turn)
        }
        "hand_started" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct H {
                hand_number: u64,
                history_complete: bool,
            }
            let h: H = serde_json::from_value(record.data).map_err(|_| "Invalid hand")?;
            let _ = h.history_complete;
            ensure(
                record.hand_id
                    == record
                        .table_id
                        .as_ref()
                        .map(|t| format!("{t}:{}", h.hand_number)),
                "Hand envelope scope mismatch",
            )?;
            ensure(hand(h.hand_number), "Invalid hand")
        }
        "room_closed" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct C {
                reconciled: bool,
            }
            let c: C = serde_json::from_value(record.data).map_err(|_| "Invalid close")?;
            ensure(!c.reconciled, "Room close cannot assert reconciliation")
        }
        "snapshot" => {
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct V {
                street: String,
                hero_cards: Vec<Option<String>>,
                board: Vec<String>,
                players: Vec<Player>,
                turn: Option<Turn>,
                history_complete: bool,
                rules_version: String,
            }
            let v: V = serde_json::from_value(record.data).map_err(|_| "Invalid snapshot")?;
            let _ = v.history_complete;
            ensure(
                v.rules_version == "wpk-observed-unverified-v1"
                    && [
                        "preflop",
                        "flop",
                        "turn",
                        "river",
                        "showdown",
                        "between_hands",
                        "unknown",
                    ]
                    .contains(&v.street.as_str()),
                "Invalid rules/street",
            )?;
            players(&v.players)?;
            ensure(
                v.hero_cards.len() == 2 && matches!(v.board.len(), 0 | 3 | 4 | 5),
                "Invalid snapshot card count",
            )?;
            ensure(
                ["preflop", "flop", "turn", "river"].contains(&v.street.as_str())
                    || v.turn.is_none(),
                "Stale terminal turn",
            )?;
            validate_text_cards(&v.hero_cards)?;
            validate_text_cards(&v.board.iter().cloned().map(Some).collect::<Vec<_>>())?;
            unique(&v.hero_cards, &v.board)?;
            v.turn.as_ref().map_or(Ok(()), turn)
        }
        _ => Err("Unknown business event".into()),
    }
}
fn validate_text_cards(cards: &[Option<String>]) -> Result<()> {
    let mut seen = std::collections::BTreeSet::new();
    for c in cards.iter().flatten() {
        let b = c.as_bytes();
        ensure(
            b.len() == 2
                && b"A23456789TJQK".contains(&b[0])
                && b"shcd".contains(&b[1])
                && seen.insert(c),
            "Invalid/duplicate cards",
        )?;
    }
    Ok(())
}
pub fn process(input: &[u8]) -> Value {
    let result = (|| -> Result<Value> {
        match serde_json::from_slice::<Request>(input).map_err(|_| "Invalid WPK input/schema")? {
            Request::Reduce {
                message,
                checkpoint,
            } => {
                let (events, state) = reduce(*message, checkpoint.map(|s| *s).unwrap_or_default())?;
                Ok(json!({"ok":true,"events":events,"checkpoint":state}))
            }
            Request::Validate { record } => {
                validate(*record)?;
                Ok(json!({"ok":true}))
            }
        }
    })();
    match result {
        Ok(value) => value,
        Err(error) => json!({"ok":false,"error":error}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_wpk_cards_map_to_unique_canonical_indices() {
        let mut indices = std::collections::BTreeSet::new();
        for suit in 1..=4 {
            for rank in 1..=13 {
                let (_, index) = card(Some(suit * 100 + rank)).unwrap().unwrap();
                assert!(indices.insert(index));
            }
        }
        assert_eq!(indices.len(), 52);
        assert_eq!(card(Some(101)).unwrap(), Some(("As".into(), 51)));
        assert_eq!(card(Some(205)).unwrap(), Some(("5h".into(), 14)));
        assert!(card(Some(-1)).unwrap().is_none());
        assert!(card(Some(414)).is_err());
    }
    #[test]
    fn unknown_fields_and_invalid_unicode_cards_fail_without_panicking() {
        let input = json!({"op":"validate","record":{"schema":"tgto.capture/0.1","type":"business_event","source_id":"synthetic","sequence":"1","captured_at":"2026-11-01T00:00:00Z","adapter_version":"wpk/1","kind":"hero_cards","table_id":"table-1","hand_id":"table-1:1","data":{"cards":["é",null]}}});
        assert_eq!(process(&serde_json::to_vec(&input).unwrap())["ok"], false);
        assert_eq!(
            process(b"{\"op\":\"reduce\",\"credential\":\"forbidden\"}")["ok"],
            false
        );
    }
}
