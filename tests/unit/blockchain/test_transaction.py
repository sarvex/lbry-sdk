from unittest import TestCase
from pickle import dumps
from binascii import hexlify, unhexlify

from lbry import Config, Ledger, Transaction
from lbry.constants import CENT, NULL_HASH32
from lbry.testcase import (
    get_transaction, get_input, get_output, get_claim_transaction
)

FEE_PER_BYTE = 50
FEE_PER_CHAR = 200000


class TestSizeAndFeeEstimation(TestCase):

    def setUp(self):
        self.ledger = Ledger(Config.with_null_dir())

    def test_output_size_and_fee(self):
        txo = get_output()
        self.assertEqual(txo.size, 46)
        self.assertEqual(txo.get_fee(self.ledger), 46 * FEE_PER_BYTE)
        claim_name = 'verylongname'
        tx = get_claim_transaction(claim_name, b'0'*4000)
        base_size = tx.size - tx.inputs[0].size - tx.outputs[0].size
        txo = tx.outputs[0]
        self.assertEqual(tx.size, 4225)
        self.assertEqual(tx.base_size, base_size)
        self.assertEqual(txo.size, 4067)
        self.assertEqual(txo.get_fee(self.ledger), len(claim_name) * FEE_PER_CHAR)
        # fee based on total bytes is the larger fee
        claim_name = 'a'
        tx = get_claim_transaction(claim_name, b'0'*4000)
        base_size = tx.size - tx.inputs[0].size - tx.outputs[0].size
        txo = tx.outputs[0]
        self.assertEqual(tx.size, 4214)
        self.assertEqual(tx.base_size, base_size)
        self.assertEqual(txo.size, 4056)
        self.assertEqual(txo.get_fee(self.ledger), txo.size * FEE_PER_BYTE)

    def test_input_size_and_fee(self):
        txi = get_input()
        self.assertEqual(txi.size, 148)
        self.assertEqual(txi.get_fee(self.ledger), 148 * FEE_PER_BYTE)

    def test_transaction_size_and_fee(self):
        tx = get_transaction()
        self.assertEqual(tx.size, 204)
        self.assertEqual(tx.base_size, tx.size - tx.inputs[0].size - tx.outputs[0].size)
        self.assertEqual(tx.get_base_fee(self.ledger), FEE_PER_BYTE * tx.base_size)


class TestAccountBalanceImpactFromTransaction(TestCase):

    def test_is_my_output_not_set(self):
        tx = get_transaction()
        with self.assertRaisesRegex(ValueError, "Cannot access net_account_balance"):
            _ = tx.net_account_balance
        tx.inputs[0].txo_ref.txo.is_my_output = True
        with self.assertRaisesRegex(ValueError, "Cannot access net_account_balance"):
            _ = tx.net_account_balance
        tx.outputs[0].is_my_output = True
        # all inputs/outputs are set now so it should work
        _ = tx.net_account_balance

    def test_paying_from_my_account_to_other_account(self):
        tx = Transaction() \
            .add_inputs([get_input(300*CENT)]) \
            .add_outputs([get_output(190*CENT, NULL_HASH32),
                          get_output(100*CENT, NULL_HASH32)])
        tx.inputs[0].txo_ref.txo.is_my_output = True
        tx.outputs[0].is_my_output = False
        tx.outputs[1].is_my_output = True
        self.assertEqual(tx.net_account_balance, -200*CENT)

    def test_paying_from_other_account_to_my_account(self):
        tx = Transaction() \
            .add_inputs([get_input(300*CENT)]) \
            .add_outputs([get_output(190*CENT, NULL_HASH32),
                          get_output(100*CENT, NULL_HASH32)])
        tx.inputs[0].txo_ref.txo.is_my_output = False
        tx.outputs[0].is_my_output = True
        tx.outputs[1].is_my_output = False
        self.assertEqual(tx.net_account_balance, 190*CENT)

    def test_paying_from_my_account_to_my_account(self):
        tx = Transaction() \
            .add_inputs([get_input(300*CENT)]) \
            .add_outputs([get_output(190*CENT, NULL_HASH32),
                          get_output(100*CENT, NULL_HASH32)])
        tx.inputs[0].txo_ref.txo.is_my_output = True
        tx.outputs[0].is_my_output = True
        tx.outputs[1].is_my_output = True
        self.assertEqual(tx.net_account_balance, -10*CENT)  # lost to fee


class TestTransactionSerialization(TestCase):

    def setUp(self):
        super().setUp()
        self.ledger = Ledger(Config.with_null_dir())

    def test_genesis_transaction(self):
        raw = unhexlify(
            "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff1f0"
            "4ffff001d010417696e736572742074696d657374616d7020737472696e67ffffffff01000004bfc91b8e"
            "001976a914345991dbf57bfb014b87006acdfafbfc5fe8292f88ac00000000"
        )
        tx = Transaction(raw)
        self.assertEqual(tx.version, 1)
        self.assertEqual(tx.locktime, 0)
        self.assertEqual(len(tx.inputs), 1)
        self.assertEqual(len(tx.outputs), 1)

        coinbase = tx.inputs[0]
        self.assertTrue(coinbase.txo_ref.is_null)
        self.assertEqual(coinbase.txo_ref.position, 0xFFFFFFFF)
        self.assertEqual(coinbase.sequence, 0xFFFFFFFF)
        self.assertIsNotNone(coinbase.coinbase)
        self.assertIsNone(coinbase.script)
        self.assertEqual(
            hexlify(coinbase.coinbase),
            b'04ffff001d010417696e736572742074696d657374616d7020737472696e67'
        )

        out = tx.outputs[0]
        self.assertEqual(out.amount, 40000000000000000)
        self.assertEqual(out.position, 0)
        self.assertTrue(out.script.is_pay_pubkey_hash)
        self.assertFalse(out.script.is_pay_script_hash)
        self.assertFalse(out.script.is_claim_involved)

        tx._reset()
        self.assertEqual(tx.raw, raw)

    def test_coinbase_transaction(self):
        raw = unhexlify(
            "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff200"
            "34d520504f89ac55a086032d217bf0700000d2f6e6f64655374726174756d2f0000000001a03489850800"
            "00001976a914cfab870d6deea54ca94a41912a75484649e52f2088ac00000000"
        )
        tx = Transaction(raw)
        self.assertEqual(tx.version, 1)
        self.assertEqual(tx.locktime, 0)
        self.assertEqual(len(tx.inputs), 1)
        self.assertEqual(len(tx.outputs), 1)

        coinbase = tx.inputs[0]
        self.assertTrue(coinbase.txo_ref.is_null)
        self.assertEqual(coinbase.txo_ref.position, 0xFFFFFFFF)
        self.assertEqual(coinbase.sequence, 0)
        self.assertIsNotNone(coinbase.coinbase)
        self.assertIsNone(coinbase.script)
        self.assertEqual(
            hexlify(coinbase.coinbase),
            b'034d520504f89ac55a086032d217bf0700000d2f6e6f64655374726174756d2f'
        )

        out = tx.outputs[0]
        self.assertEqual(out.amount, 36600100000)
        self.assertEqual(out.position, 0)
        self.assertTrue(out.script.is_pay_pubkey_hash)
        self.assertFalse(out.script.is_pay_script_hash)
        self.assertFalse(out.script.is_claim_involved)

        tx._reset()
        self.assertEqual(tx.raw, raw)

    def test_claim_transaction(self):
        raw = unhexlify(
            "01000000012433e1b327603843b083344dbae5306ff7927f87ebbc5ae9eb50856c5b53fd1d000000006a4"
            "7304402201a91e1023d11c383a11e26bf8f9034087b15d8ada78fa565e0610455ffc8505e0220038a63a6"
            "ecb399723d4f1f78a20ddec0a78bf8fb6c75e63e166ef780f3944fbf0121021810150a2e4b088ec51b20c"
            "be1b335962b634545860733367824d5dc3eda767dffffffff028096980000000000fdff00b50463617473"
            "4cdc080110011a7808011230080410011a084d616361726f6e6922002a003214416c6c207269676874732"
            "072657365727665642e38004a0052005a001a42080110011a30add80aaf02559ba09853636a0658c42b72"
            "7cb5bb4ba8acedb4b7fe656065a47a31878dbf9912135ddb9e13806cc1479d220a696d6167652f6a70656"
            "72a5c080110031a404180cc0fa4d3839ee29cca866baed25fafb43fca1eb3b608ee889d351d3573d042c7"
            "b83e2e643db0d8e062a04e6e9ae6b90540a2f95fe28638d0f18af4361a1c2214f73de93f4299fb32c32f9"
            "49e02198a8e91101abd6d7576a914be16e4b0f9bd8f6d47d02b3a887049c36d3b84cb88ac0cd2520b0000"
            "00001976a914f521178feb733a719964e1da4a9efb09dcc39cfa88ac00000000"
        )
        tx = Transaction(raw)
        self.assertEqual(tx.id, '666c3d15de1d6949a4fe717126c368e274b36957dce29fd401138c1e87e92a62')
        self.assertEqual(tx.version, 1)
        self.assertEqual(tx.locktime, 0)
        self.assertEqual(len(tx.inputs), 1)
        self.assertEqual(len(tx.outputs), 2)

        txin = tx.inputs[0]
        self.assertEqual(
            txin.txo_ref.id,
            '1dfd535b6c8550ebe95abceb877f92f76f30e5ba4d3483b043386027b3e13324:0'
        )
        self.assertEqual(txin.txo_ref.position, 0)
        self.assertEqual(txin.sequence, 0xFFFFFFFF)
        self.assertIsNone(txin.coinbase)
        self.assertEqual(txin.script.template.name, 'pubkey_hash')
        self.assertEqual(
            hexlify(txin.script.values['pubkey']),
            b'021810150a2e4b088ec51b20cbe1b335962b634545860733367824d5dc3eda767d'
        )
        self.assertEqual(
            hexlify(txin.script.values['signature']),
            b'304402201a91e1023d11c383a11e26bf8f9034087b15d8ada78fa565e0610455ffc8505e0220038a63a6'
            b'ecb399723d4f1f78a20ddec0a78bf8fb6c75e63e166ef780f3944fbf01'
        )

        # Claim
        out0 = tx.outputs[0]
        self.assertEqual(out0.amount, 10000000)
        self.assertEqual(out0.position, 0)
        self.assertTrue(out0.script.is_pay_pubkey_hash)
        self.assertTrue(out0.script.is_claim_name)
        self.assertTrue(out0.script.is_claim_involved)
        self.assertEqual(out0.script.values['claim_name'], b'cats')
        self.assertEqual(
            hexlify(out0.script.values['pubkey_hash']),
            b'be16e4b0f9bd8f6d47d02b3a887049c36d3b84cb'
        )

        # Change
        out1 = tx.outputs[1]
        self.assertEqual(out1.amount, 189977100)
        self.assertEqual(out1.position, 1)
        self.assertTrue(out1.script.is_pay_pubkey_hash)
        self.assertFalse(out1.script.is_claim_involved)
        self.assertEqual(
            hexlify(out1.script.values['pubkey_hash']),
            b'f521178feb733a719964e1da4a9efb09dcc39cfa'
        )

        tx._reset()
        self.assertEqual(tx.raw, raw)

    def test_pubkey_address(self):
        t = Transaction(unhexlify(
            "010000000100000000000000000000000000000000000000000000000000000000"
            "00000000ffffffff03510101ffffffff0100e1f505000000002321024ca653fc09"
            "4c95aa409430caf2eee08fa6e5fbbe78431e0ec9e7cd80193d98f9ac00000000"
        ))
        txo = t.outputs[0]
        self.assertEqual(txo.script.template.name, 'pay_pubkey_full')
        self.assertEqual(txo.get_address(self.ledger), 'bZi1WEjGtsdAwuZTnNNTCAZLxhHkiHec4m')

    def test_script_hash_address(self):
        t = Transaction(unhexlify(
            "020000000100000000000000000000000000000000000000000000000000000000"
            "00000000ffffffff2403b5a50b04b9e6ba5e08810007e6675b03002f504c41594d"
            "4f4e415f69735f676f6f642f000000000100c37ec60600000017a914fbbf4e9bfe"
            "70f27fb8aacd5386acc57f7a5ff7f58700000000"
        ))
        txo = t.outputs[0]
        self.assertEqual(txo.script.template.name, 'pay_script_hash')
        self.assertEqual(txo.get_address(self.ledger), 'rVBhueRT9E8RPdVcpCdXV5gRiiXVjE6VD9')

    def test_tx_with_claim_can_pickle(self):
        # used to fail with this error:
        # _pickle.PicklingError: Can't pickle <class 'lbry.blockchain.util.PUSH_SINGLE'>:
        #   attribute lookup PUSH_SINGLE on lbry.blockchain.util failed
        raw = unhexlify(
            "01000000012433e1b327603843b083344dbae5306ff7927f87ebbc5ae9eb50856c5b53fd1d000000006a4"
            "7304402201a91e1023d11c383a11e26bf8f9034087b15d8ada78fa565e0610455ffc8505e0220038a63a6"
            "ecb399723d4f1f78a20ddec0a78bf8fb6c75e63e166ef780f3944fbf0121021810150a2e4b088ec51b20c"
            "be1b335962b634545860733367824d5dc3eda767dffffffff028096980000000000fdff00b50463617473"
            "4cdc080110011a7808011230080410011a084d616361726f6e6922002a003214416c6c207269676874732"
            "072657365727665642e38004a0052005a001a42080110011a30add80aaf02559ba09853636a0658c42b72"
            "7cb5bb4ba8acedb4b7fe656065a47a31878dbf9912135ddb9e13806cc1479d220a696d6167652f6a70656"
            "72a5c080110031a404180cc0fa4d3839ee29cca866baed25fafb43fca1eb3b608ee889d351d3573d042c7"
            "b83e2e643db0d8e062a04e6e9ae6b90540a2f95fe28638d0f18af4361a1c2214f73de93f4299fb32c32f9"
            "49e02198a8e91101abd6d7576a914be16e4b0f9bd8f6d47d02b3a887049c36d3b84cb88ac0cd2520b0000"
            "00001976a914f521178feb733a719964e1da4a9efb09dcc39cfa88ac00000000"
        )
        tx = Transaction(raw)
        tx.outputs[0].script.values  # triggers parsing, needed to reproduce pickle error
        dumps(tx)