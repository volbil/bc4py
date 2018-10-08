from bc4py.config import C, V, P, BlockChainError
from bc4py.chain.block import Block
from bc4py.chain.tx import TX
from bc4py.chain.checking import check_block, check_tx, check_tx_time
from bc4py.chain.checking.signature import batch_sign_cashe
from bc4py.chain.workhash import get_workhash_fnc
from bc4py.database.builder import builder, tx_builder, user_account
from bc4py.database.create import closing, create_db
from bc4py.user.network import update_mining_staking_all_info
from bc4py.user.network.directcmd import DirectCmd
from bc4py.user.network.connection import *
from bc4py.user.exit import system_exit
from pooled_multiprocessing import mp_map_async
import logging
from time import time, sleep
import threading
from binascii import hexlify


f_working = False
f_changed_status = False
block_stack = dict()
f_staking = threading.Event()
f_staking.set()


def _generate_workhash(height, block_flag, block_b, **kwargs):
    # TODO: warning, check memory leak
    return height, get_workhash_fnc(block_flag)(block_b)


def _callback_workhash(data_list):
    if isinstance(data_list[0], str):
        logging.error("error on _callback_workhash(), {}".format(data_list[0]))
        return
    block_stack_copy = block_stack.copy()
    for height, workhash in data_list:
        if height in block_stack_copy:
            block_stack_copy[height].work_hash = workhash
    logging.debug("_callback_workhash() workhash={}".format(len(data_list)))


def batch_workhash(blocks):
    data_list = list()
    s = time()
    for block in blocks:
        if block.flag in (C.BLOCK_YES_POW, C.BLOCK_HMQ_POW, C.BLOCK_X11_POW):
            data_list.append((block.height, block.flag, block.b))
    if len(data_list) == 0:
        return
    elif len(data_list) == 1:
        height, block_flag, block_b = data_list[0]
        workhash = get_workhash_fnc(block_flag)(block_b)
        block_stack[height].work_hash = workhash
    else:
        event, result = mp_map_async(_generate_workhash, data_list, callback=_callback_workhash)
        event.wait()
        logging.debug("Success batch workhash {} {}Sec".format(len(data_list), round(time()-s, 3)))


def put_to_block_stack(r):
    block_tmp = dict()
    batch_txs = list()
    for block_b, block_height, block_flag, txs in r:
        block = Block(binary=block_b)
        block.height = block_height
        block.flag = block_flag
        for tx_b, tx_signature in txs:
            tx = TX(binary=tx_b)
            tx.height = None
            tx.signature = tx_signature
            tx_from_database = tx_builder.get_tx(txhash=tx.hash)
            if tx_from_database:
                block.txs.append(tx_from_database)
            else:
                block.txs.append(tx)
        block_tmp[block_height] = block
        batch_txs.extend(block.txs)
    # check
    batch_sign_cashe(batch_txs)
    block_stack.update(block_tmp)
    batch_workhash(tuple(block_tmp.values()))


def fill_block_stack():
    if len(block_stack) == 0:
        return
    f_staking.clear()
    height = max(block_stack.keys())+1
    logging.debug("Stack blocks on back form {}".format(height))
    r = ask_node(cmd=DirectCmd.BIG_BLOCKS, data={'height': height})
    if isinstance(r, str):
        logging.debug("NewBLockGetError:{}".format(r))
    elif isinstance(r, list):
        put_to_block_stack(r)
    else:
        logging.debug("Not correct format BIG_BLOCKS.")
    f_staking.set()


def fast_sync_chain():
    assert V.PC_OBJ is not None, "Need PeerClient start before."
    global f_changed_status
    start = time()

    # 外部Nodeに次のBlockを逐一尋ねる
    failed_num = 0
    before_block = builder.best_block
    index_height = before_block.height + 1
    logging.debug("Start sync by {}".format(before_block))
    while failed_num < 5:
        if index_height in block_stack:
            new_block = block_stack[index_height]
        else:
            if f_staking.wait(30) and index_height in block_stack:
                logging.debug("Retry from f_staking wait...")
                continue  # Get on back
            block_stack.clear()
            logging.debug("Stack blocks on front form {}".format(index_height))
            r = ask_node(cmd=DirectCmd.BIG_BLOCKS, data={'height': index_height})
            if isinstance(r, str):
                logging.debug("NewBLockGetError:{}".format(r))
                before_block = builder.get_block(before_block.previous_hash)
                index_height = before_block.height + 1
                failed_num += 1
                continue
            elif isinstance(r, list):
                put_to_block_stack(r)
                if len(block_stack) == 0:
                    break
                new_block = block_stack[index_height]
                # Get blocks on back
                if f_staking.wait(30):
                    threading.Thread(target=fill_block_stack, name='StackBlocks', daemon=True).start()
                else:
                    logging.error("Something wrong on fill_block_stack()")
                    f_staking.set()
            else:
                failed_num += 1
                logging.debug("Not correct format BIG_BLOCKS.")
                continue
        # Base check
        base_check_failed_msg = None
        if before_block.hash != new_block.previous_hash:
            base_check_failed_msg = "Not correct previous hash {}".format(new_block)
        # proof of work check
        if not new_block.pow_check():
            base_check_failed_msg = "Not correct work hash {}".format(new_block)
        # rollback
        if base_check_failed_msg is not None:
            before_block = builder.get_block(before_block.previous_hash)
            index_height = before_block.height + 1
            failed_num += 1
            for height in tuple(block_stack.keys()):
                if height >= index_height:
                    del block_stack[height]
            logging.debug(base_check_failed_msg)
            continue
        # TX check
        if len(new_block.txs) > 1:
            with closing(create_db(V.DB_ACCOUNT_PATH)) as db:
                cur = db.cursor()
                for tx in new_block.txs:
                    if tx.type in (C.TX_POS_REWARD, C.TX_POW_REWARD):
                        continue
                    check_tx(tx=tx, include_block=None)
                    tx_builder.put_unconfirmed(tx=tx, outer_cur=cur)
                db.commit()
        # Block check
        check_block(new_block)
        for tx in new_block.txs:
            tx.height = new_block.height
            check_tx(tx=tx, include_block=new_block)
        # Chainに挿入
        builder.new_block(new_block)
        for tx in new_block.txs:
            user_account.affect_new_tx(tx)
        builder.batch_apply()
        f_changed_status = True
        # 次のBlock
        failed_num = 0
        before_block = new_block
        index_height = before_block.height + 1
        # ロギング
        if index_height % 100 == 0:
            logging.debug("Update block {} now...".format(index_height + 1))
    # Unconfirmed txを取得
    logging.info("Finish get block, next get unconfirmed.")
    r = None
    while not isinstance(r, dict):
        r = ask_node(cmd=DirectCmd.UNCONFIRMED_TX, f_continue_asking=True)
    for txhash in r['txs']:
        if txhash in tx_builder.unconfirmed:
            continue
        try:
            r = ask_node(cmd=DirectCmd.TX_BY_HASH, data={'txhash': txhash}, f_continue_asking=True)
            tx = TX(binary=r['tx'])
            tx.signature = r['sign']
            check_tx_time(tx)
            check_tx(tx, include_block=None)
            tx_builder.put_unconfirmed(tx)
        except BlockChainError:
            logging.debug("Failed get unconfirmed {}".format(hexlify(txhash).decode()))
    # 最終判断
    reset_good_node()
    set_good_node()
    my_best_height = builder.best_block.height
    best_height_on_network, best_hash_on_network = get_best_conn_info()
    if best_height_on_network <= my_best_height:
        logging.info("Finish update chain data by network. {}Sec [{}<={}]"
                     .format(round(time() - start, 1), best_height_on_network, my_best_height))
        return True
    else:
        logging.debug("Continue update chain, {}<={}".format(best_height_on_network, my_best_height))
        return False


def sync_chain_loop():
    global f_working

    def loop():
        global f_changed_status, f_working
        failed = 5
        while f_working:
            check_connection()
            try:
                if P.F_NOW_BOOTING:
                    if fast_sync_chain():
                        P.F_NOW_BOOTING = False
                        if builder.best_block:
                            update_mining_staking_all_info()
                        builder.remove_failmark()
                    elif failed < 0:
                        exit_msg = 'Failed sync.'
                        builder.make_failemark(exit_msg)
                        logging.critical(exit_msg)
                        system_exit()
                        f_working = False
                    elif f_changed_status is False:
                        failed -= 1
                    elif f_changed_status is True:
                        f_changed_status = False
                    reset_good_node()
                sleep(5)
            except BlockChainError as e:
                reset_good_node()
                logging.warning('Update chain failed "{}"'.format(e))
                sleep(5)
            except BaseException as e:
                reset_good_node()
                logging.error('Update chain failed "{}"'.format(e), exc_info=True)
                sleep(5)
        # out of loop
        logging.debug("Close sync loop.")

    if f_working:
        raise Exception('Already sync_chain_loop working.')
    f_working = True
    logging.info("Start sync now {} connections.".format(len(V.PC_OBJ.p2p.user)))
    threading.Thread(target=loop, name='Sync').start()
