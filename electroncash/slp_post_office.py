import requests
import threading
import json
import sys
import time

from electroncash.networks import net
from electroncash.slp import SlpMessage, buildSendOpReturnOutput_V1
from electroncash.slp_coinchooser import SlpCoinChooser
from electroncash.transaction import Transaction

class SlpPostOfficePr:

    @staticmethod
    def build_slp_txn(coins, slp_output, receiver_output, postoffice_output, change_output):
        slp_msg = SlpMessage.parseSlpOutputScript(slp_output[1])
        outputs = [slp_output, receiver_output]
        if len(slp_msg.op_return_fields["token_output"]) - 1 == 2:
            outputs.extend([postoffice_output])
        elif len(slp_msg.op_return_fields["token_output"]) - 1 == 3:
            outputs.extend([postoffice_output, change_output])
        tx = Transaction.from_io(coins, outputs)
        return tx

    @staticmethod
    def calculate_postage_and_build_slp_msg(wallet, config, tokenId, po_data, send_amount):

        # determine the amount of postage to pay based on the token's rate and number of inputs we will sign
        weight = po_data["weight"]
        rate = None
        for stamp in po_data["stamps"]:
            if stamp["tokenId"] == tokenId:
                rate = stamp["rate"]

        if rate is None:
            raise Exception("Post Office does not offer postage for tokenId: " + tokenId)

        # variables used for txn size estimation
        slpmsg_output_max_size = 8 + 1 + 73  # case where both postage and change are needed
        slpmsg_output_mid_size = slpmsg_output_max_size - 9  # case where no token change is not needed
        slpmsg_output_min_size = slpmsg_output_mid_size - 9  # case where no token or change are needed
        output_unit_size = 34  # p2pkh output size
        input_unit_size_ecdsa = 149  # approx. size needed for ecdsa signed input
        input_unit_size_schnorr = 141  # approx. size needed for schnorr signed input
        txn_overhead = 4 + 1 + 1 + 4  # txn version, input count varint, output count varint, timelock

        # determine number of stamps required in this while loop
        sats_diff_w_fee = 1  # controls entry into while loop
        stamp_count = -1  # will get updated to 0 stamps in first iteration
        while sats_diff_w_fee > 0:
            stamp_count += 1
            coins, _ = SlpCoinChooser.select_coins(wallet, tokenId, (send_amount + (rate * stamp_count)), config)

            output_dust_count = 1
            slpmsg_output_size = slpmsg_output_min_size
            postage_amt = rate * stamp_count
            total_coin_value = 0
            for coin in coins:
                total_coin_value += coin["token_value"]
                wallet.add_input_info(coin)
            change_amt = total_coin_value - send_amount - postage_amt

            if postage_amt > 0 and change_amt > 0:
                output_dust_count = 3
                slpmsg_output_size = slpmsg_output_max_size
            elif postage_amt > 0 or change_amt > 0:
                output_dust_count = 2
                slpmsg_output_size = slpmsg_output_mid_size

            txn_size_wo_stamps = txn_overhead + input_unit_size_ecdsa * len(
                coins) + output_unit_size * output_dust_count + slpmsg_output_size

            # output cost differential (positive value means we need stamps)
            output_sats_diff = (output_dust_count * 546) - (len(coins) * 546)

            # fee cost differential (positive value means we need more stamps)
            fee_rate = 1
            sats_diff_w_fee = (txn_size_wo_stamps * fee_rate) + output_sats_diff - stamp_count * weight

        if output_dust_count == 1:
            amts = [send_amount]
            needs_postage = False
        elif output_dust_count == 2 and postage_amt > 0:
            amts = [send_amount, postage_amt]
            needs_postage = True
        elif output_dust_count == 2 and change_amt > 0:
            amts = [send_amount, change_amt]
            needs_postage = False
        elif output_dust_count == 3:
            amts = [send_amount, postage_amt, change_amt]
            needs_postage = True
        else:
            raise Exception("Unhandled exception")

        slp_output = buildSendOpReturnOutput_V1(tokenId, amts)

        return coins, slp_output, needs_postage, postage_amt


class SlpPostOffice:

    @staticmethod
    def build_slp_txn(coins, slp_output, pre_postage_outputs, postoffice_output, change_output, send_amount, old_slp_msg):
        slp_msg = SlpMessage.parseSlpOutputScript(slp_output[1])
        pre_postage_outputs = list(pre_postage_outputs[1:])
        if sum(old_slp_msg.op_return_fields["token_output"]) > send_amount:  # has change output
            pre_postage_outputs = pre_postage_outputs[:-1]

        outputs = [slp_output] + pre_postage_outputs
        if len(slp_msg.op_return_fields["token_output"]) - len(pre_postage_outputs) == 2:
            outputs.extend([postoffice_output])
        elif len(slp_msg.op_return_fields["token_output"]) - len(pre_postage_outputs) == 3:
            outputs.extend([postoffice_output, change_output])
        tx = Transaction.from_io(coins, outputs)
        return tx

    @staticmethod
    def sign_slp_txn(tx):
        #assert SlpTransactionChecker.check_tx_slp(self.wallet, tx, coins_to_burn=slp_coins_to_burn, amt_to_burn=slp_amt_to_burn)
        pass

    @staticmethod
    def calculate_postage_and_build_slp_msg(wallet, config, tokenId, po_data, send_amount, old_slp_msg):

        # determine the amount of postage to pay based on the token's rate and number of inputs we will sign
        weight = po_data["weight"]
        rate = None
        for stamp in po_data["stamps"]:
            if stamp["tokenId"] == tokenId:
                rate = stamp["rate"]

        if rate is None:
            raise Exception("Post Office does not offer postage for tokenId: " + tokenId)

        pre_postage_amts = list(old_slp_msg.op_return_fields["token_output"][1:])
        if sum(pre_postage_amts) > send_amount:  # has change output
            pre_postage_amts = pre_postage_amts[:-1]
        min_dust_count = len(pre_postage_amts)

        # variables used for txn size estimation
        slpmsg_output_min_size = 8 + 1 + 46 + 9 * min_dust_count  # case where no postage or change are needed
        slpmsg_output_mid_size = slpmsg_output_min_size + 9       # case where change is not needed
        slpmsg_output_max_size = slpmsg_output_mid_size + 9       # case where both postage and change are needed
        output_unit_size = 34                                     # p2pkh output size
        input_unit_size_ecdsa = 149                               # approx. size needed for ecdsa signed input
        input_unit_size_schnorr = 141                             # approx. size needed for schnorr signed input
        txn_overhead = 4 + 1 + 1 + 4                              # txn version, input count varint, output count varint, timelock

        # determine number of stamps required in this while loop
        sats_diff_w_fee = 1    # controls entry into while loop
        stamp_count = -1        # will get updated to 0 stamps in first iteration
        while sats_diff_w_fee > 0:
            stamp_count += 1
            coins, _ = SlpCoinChooser.select_coins(wallet, tokenId, (send_amount + (rate * stamp_count)), config)

            output_dust_count = min_dust_count
            slpmsg_output_size = slpmsg_output_min_size
            postage_amt = rate * stamp_count
            total_coin_value = 0
            for coin in coins:
                total_coin_value += coin["token_value"]
                wallet.add_input_info(coin)
            change_amt = total_coin_value - send_amount - postage_amt

            if postage_amt > 0 and change_amt > 0:
                output_dust_count += 2
                slpmsg_output_size = slpmsg_output_max_size
            elif postage_amt > 0 or change_amt > 0:
                output_dust_count = 1
                slpmsg_output_size = slpmsg_output_mid_size
            
            txn_size_wo_stamps = txn_overhead + input_unit_size_ecdsa * len(coins) + output_unit_size * output_dust_count + slpmsg_output_size

            # output cost differential (positive value means we need stamps)
            output_sats_diff = (output_dust_count * 546) - (len(coins) * 546)

            # fee cost differential (positive value means we need more stamps)
            fee_rate = 1
            sats_diff_w_fee = (txn_size_wo_stamps * fee_rate) + output_sats_diff - stamp_count * weight

        dust_count_diff = output_dust_count - min_dust_count
        if dust_count_diff == 0:
            amts = pre_postage_amts
            needs_postage = False
        elif dust_count_diff == 1 and postage_amt > 0:
            amts = pre_postage_amts + [postage_amt]
            needs_postage = True
        elif dust_count_diff == 1 and change_amt > 0:
            amts = pre_postage_amts + [change_amt]
            needs_postage = False
        elif dust_count_diff == 2:
            amts = pre_postage_amts + [postage_amt, change_amt]
            needs_postage = True
        else:
            raise Exception("Unhandled exception")

        slp_output = buildSendOpReturnOutput_V1(tokenId, amts)

        return coins, slp_output, needs_postage, postage_amt

    @staticmethod
    def sign_inputs_for_po_server(tx, wallet):
        """
        Signs and returns incomplete transaction for a post office to complete
        """
        # TODO
        return

    @staticmethod
    def sign_inputs_from_payment_request(pr, wallet):
        """
        Signs and returns incomplete transaction for a payment request
        """
        # TODO
        return

class _SlpPostOfficeClient:
    """
    An SLP post office client to interact with a single post office server.
    """
    def __init__(self, update_data_interval=100):

        self._gui_object = None
        self.post_office_hosts = net.POST_OFFICE_SERVERS

        self.ban_list = []
        self.postage_data = {}
        self.optimized_rates = {}
        self.update_data_interval = update_data_interval

        self.fetch_thread = threading.Thread(target=self.mainloop, name='SlpPostOfficeClient', daemon=True)
        self.fetch_thread.start()

    def _set_postage(self, host, _json):
        try:
            j = json.loads(_json)
        except json.decoder.JSONDecodeError:
            if host in self.postage_data.keys():
                self.postage_data.pop(host)
        else:
            self.postage_data[host] = j

    def _fetch_postage_json(self, host):
        res = requests.get(host + "/postage", timeout=5)
        self._set_postage(host, res.text)

    def bind_gui(self, gui):
        self._gui_object = gui

    def mainloop(self):
        try:
            while True:
                # wait first so the electrum gui binds
                time.sleep(self.update_data_interval)

                # after the electrum gui binds we can check the user's po config
                enabled = False
                if self._gui_object:
                    enabled = self._gui_object().config.get('slp_post_office_enabled', False)

                # fetch postage rates
                if enabled:
                    for host in self.post_office_hosts:
                        try:
                            self._fetch_postage_json(host)
                            self.optimize_rates()
                        except:
                            print(
                                "[SLP Post Office Client]: Failed to retrieve postage data from %s . Will retry in %d seconds." %
                                (host, self.update_data_interval)
                            )
        finally:
            print("[SLP Post Office Client] Error: mainloop exited.", file=sys.stderr)

    def optimize_rates(self):
        token_rates = {}
        for host in self.postage_data.keys():
            try:
                stamps = self.postage_data[host]["stamps"]
            except KeyError:
                continue
            else:
                for stamp in stamps:
                    stamp['host'] = host
                    stamp['rate'] = int(stamp['rate'])
                    tokenId = stamp["tokenId"]
                    if tokenId not in token_rates.keys():
                        token_rates[tokenId] = []
                    token_rates[tokenId].append(stamp)
        
        for token in token_rates:
            sorted(token_rates[token], key=lambda i: i['rate'])
        for token in token_rates.keys():
            token_rates[token] = self.postage_data[token_rates[token][0]['host']]
        self.optimized_rates = token_rates

    def get_optimized_postage_data_for_token(self, token_id):
        return self.optimized_rates.get(token_id)

    def get_optimized_post_office_url_for_token(self, token_id):
        least_priced_stamp = self.optimized_rates[token_id]['stamps'][0]
        return least_priced_stamp['host'] + '/postage'

    def ban_post_office(self, url):
        if not url in self.ban_list:
            self.ban_list.append(url)

    def allow_post_office(self, url):
        if url in self.ban_list:
            self.ban_list.remove(url)

slp_po = _SlpPostOfficeClient()