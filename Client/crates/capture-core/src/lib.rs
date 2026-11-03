//! Browser-independent capture checks and WPK observation normalization.
mod wpk;
use std::sync::Mutex;

struct Buffers {
    input: Vec<u8>,
    output: Vec<u8>,
}
static BUFFERS: Mutex<Buffers> = Mutex::new(Buffers {
    input: Vec::new(),
    output: Vec::new(),
});

/// The JS host writes exactly len initialized bytes, then calls process_wpk.
#[unsafe(no_mangle)]
pub extern "C" fn prepare_input(len: u32) -> u32 {
    if len > 256 * 1024 {
        return 0;
    }
    let mut b = BUFFERS.lock().expect("single threaded host");
    b.input.fill(0);
    b.input.resize(len as usize, 0);
    b.output.clear();
    b.input.as_mut_ptr() as usize as u32
}
#[unsafe(no_mangle)]
pub extern "C" fn process_wpk() -> u32 {
    let mut b = BUFFERS.lock().expect("single threaded host");
    b.output =
        serde_json::to_vec(&wpk::process(&b.input)).unwrap_or_else(|_| b"{\"ok\":false}".to_vec());
    b.output.len() as u32
}
#[unsafe(no_mangle)]
pub extern "C" fn output_ptr() -> u32 {
    BUFFERS
        .lock()
        .expect("single threaded host")
        .output
        .as_ptr() as usize as u32
}
#[unsafe(no_mangle)]
pub extern "C" fn clear_buffers() {
    let mut b = BUFFERS.lock().expect("single threaded host");
    b.input.fill(0);
    b.output.fill(0);
    b.input.clear();
    b.output.clear();
}

/// Stable scalar ABI for the initial validation spike.
#[unsafe(no_mangle)]
pub extern "C" fn abi_version() -> u32 {
    3
}

/// Validate complete data/control frames before storage; RFC control payloads are <=125 bytes.
#[unsafe(no_mangle)]
pub extern "C" fn validate_frame(opcode: u32, bytes: u32, limit: u32) -> u32 {
    u32::from(!matches!(opcode, 1 | 2 | 8 | 9 | 10))
        | (u32::from(bytes > limit) << 1)
        | (u32::from(opcode >= 8 && bytes > 125) << 2)
}

/// 0=advance, 1=duplicate, 2=regression, 3=beyond the current sent batch.
#[unsafe(no_mangle)]
pub extern "C" fn validate_ack(last: u64, sent: u64, ack: u64) -> u32 {
    if ack > sent {
        3
    } else if ack < last {
        2
    } else if ack == last {
        1
    } else {
        0
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn fits_budget(current: u64, added: u64, limit: u64) -> u32 {
    u32::from(
        current
            .checked_add(added)
            .is_some_and(|total| total <= limit),
    )
}

/// Card i uses bit i (0..52); counts preserve duplicate information lost by a bitset.
/// Returns flags: 1=invalid bit, 2=hero count, 4=board count, 8=count mismatch, 16=overlap.
#[unsafe(no_mangle)]
pub extern "C" fn validate_cards(
    hero_mask: u64,
    board_mask: u64,
    hero_count: u32,
    board_count: u32,
) -> u32 {
    let mut errors = 0;
    if (hero_mask | board_mask) >> 52 != 0 {
        errors |= 1;
    }
    if !matches!(hero_count, 0 | 2) {
        errors |= 2;
    }
    if !matches!(board_count, 0 | 3 | 4 | 5) {
        errors |= 4;
    }
    if hero_mask.count_ones() != hero_count || board_mask.count_ones() != board_count {
        errors |= 8;
    }
    if hero_mask & board_mask != 0 {
        errors |= 16;
    }
    errors
}

/// 0=next contiguous, 1=duplicate/older, 2=gap.
/// Caller must verify identical content before treating a repeated sequence as an ACK retry.
#[unsafe(no_mangle)]
pub extern "C" fn assess_sequence(last: u64, next: u64) -> u32 {
    if next <= last {
        return 1;
    }
    if next - last == 1 { 0 } else { 2 }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frame_ack_and_budget_boundaries() {
        assert_eq!(validate_frame(2, 1024, 1024), 0);
        assert_eq!(validate_frame(1, 1025, 1024), 2);
        assert_eq!(validate_frame(3, 0, 1024), 1);
        assert_eq!(validate_frame(9, 125, 1024), 0);
        assert_eq!(validate_frame(9, 126, 1024), 4);
        assert_eq!(validate_ack(4, 8, 8), 0);
        assert_eq!(validate_ack(4, 8, 4), 1);
        assert_eq!(validate_ack(4, 8, 3), 2);
        assert_eq!(validate_ack(4, 8, 9), 3);
        assert_eq!(fits_budget(4, 6, 10), 1);
        assert_eq!(fits_budget(u64::MAX, 1, u64::MAX), 0);
    }

    #[test]
    fn unknown_cards_and_a_valid_flop_are_accepted() {
        assert_eq!(validate_cards(0, 0, 0, 0), 0);
        assert_eq!(validate_cards(3, 28, 2, 3), 0);
        assert_eq!(validate_cards(3, 124, 2, 5), 0);
    }

    #[test]
    fn duplicates_are_not_lost_by_bitset_conversion() {
        assert_ne!(validate_cards(1, 0, 2, 0) & 8, 0);
        assert_ne!(validate_cards(3, 7, 2, 3) & 16, 0);
    }

    #[test]
    fn invalid_counts_and_out_of_deck_bits_are_rejected() {
        assert_ne!(validate_cards(1, 0, 1, 0) & 2, 0);
        assert_ne!(validate_cards(0, 3, 0, 2) & 4, 0);
        assert_ne!(validate_cards(1 << 52, 0, 0, 0) & 1, 0);
    }

    #[test]
    fn contiguous_duplicate_and_gap_are_distinct() {
        assert_eq!(assess_sequence(0, 1), 0);
        assert_eq!(assess_sequence(7, 8), 0);
        assert_eq!(assess_sequence(7, 7), 1);
        assert_eq!(assess_sequence(7, 6), 1);
        assert_eq!(assess_sequence(7, 9), 2);
    }

    #[test]
    fn sequence_never_wraps() {
        assert_eq!(assess_sequence(u64::MAX, 0), 1);
        assert_eq!(assess_sequence(u64::MAX - 1, u64::MAX), 0);
    }
}
