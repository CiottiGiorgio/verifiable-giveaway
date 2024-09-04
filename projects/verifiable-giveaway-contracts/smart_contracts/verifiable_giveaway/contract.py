from typing import Literal, Tuple

from algopy import (
    Application,
    ARC4Contract,
    BigUInt,
    Bytes,
    Global,
    LocalState,
    OnCompleteAction,
    OpUpFeeSource,
    TemplateVar,
    Txn,
    UInt64,
    arc4,
    ensure_budget,
    op,
    subroutine,
    urange,
)
from lib_pcg import pcg128_init, pcg128_random

import smart_contracts.verifiable_giveaway.config as cfg
import smart_contracts.verifiable_giveaway.errors as err

# Safety argument:
# We desire to produce ordered k-permutations of the participants where n == participants, k == winners.
# The algorithm that we use to generate k-permutations wouldn't change if we wished to generate combinations.
# Therefore, the user could (wrongly) assume that it's equally safe to generate k-permutations or combination.
# This is not the case.
# The number of ordered k-permutations is n! / (n-k)!.
# The number of unordered combinations is (n choose k) == n! / ((n-k)!k!).
# In the worst case, the number of possible k-permutations is uncontrollably larger than the number of
#  possible combinations.
# The conceptual distinction between ordered k-permutations and unordered combinations is also difficult to grasp for
#  a general audience.

# For these reasons, we will make our safety arguments and advertise the features of this smart contract
#  in terms of k-permutations.
# Since we are using pseudo-randomness, we need to guarantee that our randomness seed size is appropriate for
#  the number of possible k-permutations.
# The seed not only needs to be strictly larger, but also overwhelmingly larger to prevent collisions in
#  the irregular mapping from seeds to k-permutations.
# https://en.wikipedia.org/wiki/Fisher%E2%80%93Yates_shuffle#Pseudorandom_generators

# Let's define some bounds on our inputs:
# The number of participants must be greater or equal to the number of winners (n >= k).
# A single seed produced with the Randomness Beacon is 256-bit.
# We want the ratio between the number of distinct pseudo-random sequences and
#  the number of k-permutations to be at least 2^128 (overwhelmingly larger).

# #distinct_prn_sequences / #k_permutations >= 2^128
# log2(#distinct_prn_sequences) - log2(#k_permutations) >= 128
# log2(#k_permutations) <= log2(2^256) - 128
# log2(#k_permutations) <= 128

# log2(n! / (n-k)!) = log2(n * (n-1) * ... * (n-k+1)) = log2(n) + log2(n-1) + ... + log2(n-k+1)
# In natural language, the sum of the logarithms of the numbers from n to n-k+1.
# This sum must be less than or equal to 128.

# For any #participants == n, the number of possible k-permutations is minimized by k == 0.
# For our purposes, k is at least 1 so let's consider k == 1.
# Since:
# - log2(n! / (n-1)!) = log2(n)
# - log2(2^128 - 1) ~ 127,999
# - log2(2^128) = 128
# - log2(2^128 + 1) > 128
# When n >= 2^128 + 1, there does not exist an admissible k such that #k-permutations is safe to compute
#  with a 128-bit seed.
# Therefore, n fits in a 256-bit integer.

# However, this an enormous number of participants and would allow for a single winner at most.
# It would also force us to use BigUInt math everywhere and that's expensive.
# If participants was a 64-bit number, it would allow for 2 winners at most, but we could use native uint64 math.
# We are going to assume that participants is a 32-bit number because it allows 4 winners in the worst case
#  and native uint64 math.
# We also require the number of participants to be less than 2^32-1 because we want to shuffle an array
#  of values from 1 to n+1 (not from 0 to n).
# If n = 2^32-1, then the last element is 2^32 which is unrepresentable with 32-bit.

# For any #winners == k, the number of possible k-permutations is minimized by n == k.
# Since:
# - log2(k! / (k-k)!) = log2(k!)
# - log2(34!) ~ 127,795
# - log2(35!) ~ 132,924
# When k >= 35, there does not exist an admissible n such that #k-permutations is safe to compute
#  with a 128-bit seed.
# Therefore, k fits in an 8-bit integer.


class Commitment(arc4.Struct, kw_only=True):
    tx_id: arc4.StaticArray[arc4.Byte, Literal[32]]
    round: arc4.UInt64
    participants: arc4.UInt32
    winners: arc4.UInt8


class Reveal(arc4.Struct, kw_only=True):
    commitment_tx_id: arc4.StaticArray[arc4.Byte, Literal[32]]
    winners: arc4.DynamicArray[arc4.UInt32]


# TODO: We need more unit testing on the numerical stability of this algorithm.
# TODO: We need more error analysis given the bits used for n and the bits of the fractional
#  part we wish to compute.
# It was already attempted to re-write this function in terms of BigUInt math to allow for
#  larger arguments.
# That version was much more expensive from an opcode budget perspective.
# If today's precision is acceptable and within reasonable opcode economy, it's best to leave it as is.
# https://mathsanew.com/articles/computing_logarithm_bit_by_bit.pdf
# https://en.wikipedia.org/wiki/Binary_logarithm#Iterative_approximation
@subroutine
def binary_logarithm(n: UInt64, m: UInt64) -> UInt64:
    """Approximates the binary logarithm of an integer using an iterative approach.

    This function computes the integer part of the logarithm by looking at the Most Set Bit (MSB).
    The fractional part of the logarithm is approximated by discovering one bit with each iteration.

    The underlying property that we are leveraging is that log(x^2) == 2log(x), which means we
     can iteratively square the argument and discover the next fractional bit of the logarithm.

    This algorithm is sensitive to the precision of the input integer. The more bits of the argument,
     the better the result.
    The integer can be divided by 2 with each iteration, in which case we lose one bit of information.
    Squaring doesn't add any bit of information even if it makes the number larger.
    For this reason, we try to use this function with the largest uint64 possible.

    The algorithm assumes that n is divided by the largest power of two it can take before computing
     the fractional part of the logarithm (this will always be 2^(integer part)).
    Stated alternatively, the fractional part assumes the input is between 1 and 2 (not included).
    It's at least one because we are computing the logarithm of a natural number.
    It's less than two because, if it wasn't, it could've been divided by a larger power of two.

    As we don't have access to floats and would like to use the cheapest AVM math available,
     we can't divide n by the largest power of two it can take.
    Instead, we will interpret n as a fixed point number with an implicit scaling factor of that same
     largest power of two.
    We can safely and cheaply compute any math involving 64-bit fix point numbers thanks to wide arithmetic.

    It should be noted that squaring any number between (1, 2] could result in any number between (1, 4].
    Therefore, for n >= sqrt(2) and scaling factor = 2^64, the result will be a 65-bit number.
    Stated alternatively, if n >= sqrt(2) then n^2 >= 2 which means the result needs 64 bits to fit.
    63 bits for the fractional part and 2 bits for the integer part.
    Everytime n >= 2, we divide it by 2 so we never exceed 65 bits ever.

    With all this in mind, the argument to this function should be the largest integer that fits into 63 bits.

    Args:
        n: The integer that is the argument of the binary logarithm
        m: The numer of iterations to compute the fractional part or, equivalently,
            the number of bits to compute of the fractional part

    Returns:
        A fixed point number with an implicit scaling factor of 2^m.
    """
    integer_component = op.bitlen(n) - 1

    # If n was a float at this point, this would be:
    # if n == 1:
    if n == (1 << integer_component):
        return integer_component << m

    fractional_component = UInt64(0)
    for _i in urange(m):
        fractional_component <<= 1
        # n *= n
        square_high, square_low = op.mulw(n, n)
        n = op.divw(square_high, square_low, 1 << integer_component)
        # if n >= 2:
        if n >= (2 << integer_component):
            fractional_component |= 1
            # n /= 2
            n >>= 1

    return (integer_component << m) | fractional_component


# TODO: We need more unit testing on the numerical stability of this algorithm.
@subroutine
def sum_logs(start: UInt64, end: UInt64, m: UInt64) -> UInt64:
    """Finds a numerically stable way to compute the binary logarithm of the #k-permutations.

    Recall that:
    log2(n! / (n-k)!) = log2(n * (n-1) * ... * (n-k+1)) = log2(n) + log2(n-1) + ... + log2(n-k+1)

    For reasons highlighted in the binary_logarithm docstring, we want to call it with the largest integer
     that fits in 63 bits.

    Following from the log laws:
    log2(n) + ... + log2(n-3) = log2(n * (n-1)) + log2((n-2) * (n-3))

    This loop will do the product of the consecutive integers until it's too big and only then compute the binary log.
    Also, this allows us to reduce the number of calls to binary_logarithm.

    Args:
        start: The first integer in the product (included)
        end: The last integer outside the product (not included)
        m: The numer of iterations to compute the fractional part or, equivalently,
            the number of bits to compute of the fractional part

    Returns:
        A fixed point number with an implicit scaling factor of 2^m.
    """
    log_arg = UInt64(1)
    sum_of_logs = UInt64(0)

    for i in urange(start, end):
        overflow, low = op.mulw(log_arg, i)
        if overflow or op.bitlen(low) == 64:
            sum_of_logs += binary_logarithm(log_arg, m)
            log_arg = i
        else:
            log_arg *= i
    sum_of_logs += binary_logarithm(log_arg, m)

    return sum_of_logs


class VerifiableGiveaway(ARC4Contract):
    def __init__(self) -> None:
        self.commitment = LocalState(Commitment)

    @arc4.baremethod(allow_actions=[OnCompleteAction.UpdateApplication])
    def update(self) -> None:
        assert Txn.sender == Global.creator_address, err.CREATOR

    @arc4.baremethod(allow_actions=[OnCompleteAction.DeleteApplication])
    def delete(self) -> None:
        assert Txn.sender == Global.creator_address, err.CREATOR

    # We need these getters because we are using template values.
    # If we store the template value in global state for easy reading,
    #  the state won't change automatically after a contract update.
    @arc4.abimethod(readonly=True)
    def get_templated_randomness_beacon_id(self) -> UInt64:
        return TemplateVar[Application](cfg.RANDOMNESS_BEACON).id

    @arc4.abimethod(readonly=True)
    def get_templated_safety_round_gap(self) -> UInt64:
        return TemplateVar[UInt64](cfg.SAFETY_GAP)

    @arc4.abimethod(allow_actions=[OnCompleteAction.NoOp, OnCompleteAction.OptIn])
    def commit(
        self, delay: arc4.UInt8, participants: arc4.UInt32, winners: arc4.UInt8
    ) -> None:
        assert TemplateVar[UInt64](cfg.SAFETY_GAP) <= delay.native, err.SAFE_GAP

        assert 1 <= winners.native < 35, err.WINNERS_BOUND
        assert 2 <= participants.native < 2**32-1, err.PARTICIPANTS_BOUND
        assert winners.native <= participants.native, err.INPUT_SOUNDNESS

        ensure_budget(
            winners.native * TemplateVar[UInt64]("COMMIT_OPUP_SCALING_COST_CONSTANT"),
            OpUpFeeSource.GroupCredit,
        )

        sum_of_logs = sum_logs(
            participants.native - winners.native + 1,
            participants.native + 1,
            TemplateVar[UInt64](cfg.LOG_PRECISION),
        )
        assert sum_of_logs <= (
            128 << TemplateVar[UInt64](cfg.LOG_PRECISION)
        ), err.SAFE_SIZE

        self.commitment[Txn.sender] = Commitment(
            tx_id=arc4.StaticArray[arc4.Byte, Literal[32]].from_bytes(Txn.tx_id),
            round=arc4.UInt64(Global.round + delay.native),
            participants=participants,
            winners=winners,
        )

    @arc4.abimethod(allow_actions=[OnCompleteAction.NoOp, OnCompleteAction.CloseOut])
    def reveal(self) -> Reveal:
        commitment = self.commitment[Txn.sender].copy()
        del self.commitment[Txn.sender]

        committed_participants = commitment.participants.native
        committed_winners = commitment.winners.native

        assert Global.round >= commitment.round.native, err.ROUND_ELAPSED

        vrf_output, _txn = arc4.abi_call[arc4.DynamicBytes](
            "must_get",
            commitment.round,
            commitment.tx_id.bytes,
            app_id=TemplateVar[Application](cfg.RANDOMNESS_BEACON).id,
        )

        for i in urange(TemplateVar[UInt64](cfg.BINS)):
            op.Scratch.store(i, Bytes())

        # FIXME: We should check how much fee was provided for this call. If it's too much it's a draining attack
        #  and the contract should protect the user/funding account.
        ensure_budget(
            committed_winners
            * TemplateVar[UInt64]("REVEAL_OPUP_SCALING_COST_CONSTANT"),
            OpUpFeeSource.GroupCredit,
        )

        # Knuth shuffle.
        # We don't create a pre-initialized array of elements from 1 to n because
        #  that could easily exceed the stack element size limit.
        # Instead, we assume that at position i lies the number i + 1.
        # Where that element has been changed, we will look it up in a dict-like data structure based on scratch space.

        # We want to stop after "winners" iterations unless "winners" == "participants"
        #  in which case we want to stop at "winners" - 1.
        # We never need to shuffle the last element because it would just end up in the same position.
        n_shuffles = (
            committed_winners
            # We know that, by construction, "winners" <= "participants".
            if committed_winners < committed_participants
            else committed_winners - 1
        )
        state = pcg128_init(vrf_output.native)
        winners = arc4.DynamicArray[arc4.UInt32]()
        for i in urange(n_shuffles):
            state, sequence = pcg128_random(
                state,
                BigUInt(i),
                BigUInt(committed_participants),
                UInt64(1),
            )
            j = op.extract_uint32(sequence[0].bytes, 12)

            i_bin = op.Scratch.load_bytes(i % TemplateVar[UInt64](cfg.BINS))
            i_found, i_pos, i_value = linear_search(i_bin, i)

            j_bin = op.Scratch.load_bytes(j % TemplateVar[UInt64](cfg.BINS))
            j_found, j_pos, j_value = linear_search(j_bin, j)

            winners.append(arc4.UInt32(j_value if j_found else j + 1))

            if j_found:
                j_bin = op.replace(
                    j_bin, j_pos + 4, arc4.UInt32(i_value if i_found else i + 1).bytes
                )
            else:
                j_bin += (
                    arc4.UInt32(j).bytes
                    + arc4.UInt32(i_value if i_found else i + 1).bytes
                )
            op.Scratch.store(j % TemplateVar[UInt64](cfg.BINS), j_bin)

        if committed_participants == committed_winners:
            found, _pos, last_winner = linear_search(
                op.Scratch.load_bytes(
                    (committed_winners - UInt64(1)) % TemplateVar[UInt64](cfg.BINS)
                ),
                committed_winners - UInt64(1),
            )
            winners.append(arc4.UInt32(last_winner if found else committed_winners))

        return Reveal(
            commitment_tx_id=commitment.tx_id.copy(),
            winners=winners.copy(),
        )


@subroutine
def linear_search(bin_list: Bytes, key: UInt64) -> Tuple[bool, UInt64, UInt64]:
    for i in urange(UInt64(0), bin_list.length, UInt64(8)):
        bin_key = op.extract_uint32(bin_list, i)
        if bin_key == key:
            return True, i, op.extract_uint32(bin_list, i + 4)
    return False, UInt64(0), UInt64(0)
