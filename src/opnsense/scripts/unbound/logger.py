#!/usr/local/bin/python3

"""
    Copyright (c) 2022 Deciso B.V.
    All rights reserved.

    Redistribution and use in source and binary forms, with or without
    modification, are permitted provided that the following conditions are met:

    1. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.

    2. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.

    THIS SOFTWARE IS PROVIDED ``AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES,
    INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
    AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY,
    OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
    SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
    INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
    CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
    ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
    POSSIBILITY OF SUCH DAMAGE.
"""

import sys
import selectors
import argparse
import syslog
import time
import datetime
import pandas
import signal
import socket
import duckdb
sys.path.insert(0, "/usr/local/opnsense/site-python")
from duckdb_helper import DbConnection

class DNSReader:
    def __init__(self, target_pipe, flush_interval):
        self.target_pipe = target_pipe
        self.timer = 0
        self.cleanup_timer = 0
        self.flush_interval = flush_interval
        self.buffer = list()
        self.selector = selectors.DefaultSelector()
        self.fd = None

        self.client_map = {}
        self.update_clients = set()
        self.update_hostname = False

    def _resolve_ip(self, ip, timeout=0.01):
        # if a host is known locally, we should be able to resolve it sub 10ms
        if ip is None:
            return
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            host = socket.gethostbyaddr(ip)[0]
        except Exception:
            host = None
        socket.setdefaulttimeout(old)
        return host

    def _setup_db(self):
        with DbConnection('/var/unbound/data/unbound.duckdb', read_only=False) as db:
            db.connection.execute("""
                CREATE TABLE IF NOT EXISTS query (
                    uuid UUID,
                    time INTEGER,
                    client TEXT,
                    family TEXT,
                    type TEXT,
                    domain TEXT,
                    action INTEGER,
                    source INTEGER,
                    blocklist TEXT,
                    rcode INTEGER,
                    resolve_time_ms INTEGER,
                    dnssec_status INTEGER,
                    ttl INTEGER
                )
            """)

            db.connection.execute("""
                CREATE TABLE IF NOT EXISTS client (
                    ipaddr TEXT UNIQUE,
                    hostname TEXT
                );

                DELETE FROM client
            """)

            for size in [600, 300, 60]:
                db.connection.execute(
                    """
                        CREATE OR REPLACE VIEW v_time_series_{min}min AS (
                            SELECT
                                epoch(to_timestamp(epoch(now()) - epoch(now()) % {intv}) -
                                    (i.generate_series * INTERVAL {min} MINUTE)) as start_timestamp,
                                epoch(to_timestamp(epoch(now()) - epoch(now()) % {intv}) -
                                    ((i.generate_series - 1) * INTERVAL {min} MINUTE)) as end_timestamp
                            FROM
                                generate_series(0, {lim}) as i
                        )
                    """.format(intv=size, min=(size//60), lim=((60//(size//60))*24))
                )

            # when an earlier version created an index, make sure it doesn't exists.
            # We do need to read the file anyway and adding an index means a **lot** of maintenance in the logger.
            db.connection.execute("DROP INDEX IF EXISTS idx_query")

    def _sig(self, *args):
        # signal either open() or select() to gracefully close.
        # we intentionally raise an exception since syscalls such as select()
        # will default to a retry with a computed timeout if no exception is thrown
        # as per PEP 475. This will force select() or open() to return so we can free up any resources.
        raise InterruptedError()

    def close_logger(self):
        syslog.syslog(syslog.LOG_NOTICE, "Closing logger")
        # we might be killing the process before open() has had a chance to return,
        # so check if the file descriptor is there
        if self.fd is not None:
            self.selector.unregister(self.fd)
            self.fd.close()
        self.selector.close()
        sys.exit(0)

    def _read(self, fd, mask):
        r = self.fd.readline()
        if r == '':
            return False

        q = tuple(r.strip("\n").split('|'))
        self.buffer.append(q)

        self.update_clients.add(q[2])

        # Start a transaction every flush_interval seconds. With regular inserts
        # we would also need to limit the amount of queries we buffer before inserting them,
        # but appending a DataFrame of N size is always faster than splitting N into chunks and
        # individually appending them in separate DataFrames.
        now = time.time()
        if (now - self.timer) > self.flush_interval:
            # A note on the usage of DbConnection:
            # The pipe fifo can fill up while blocking for the lock, since during this time the
            # read() callback cannot run to empty it. The dnsbl module side catches this with
            # a BlockingIOError, forcing it to re-buffer the query, making the process
            # "eventually consistent". Realistically this condition should never occur.
            with DbConnection('/var/unbound/data/unbound.duckdb', read_only=False) as db:
                self.timer = now
                if (now - self.cleanup_timer) > 3600:
                    self.cleanup_timer = now
                    # truncate the database to the last 7 days once an hour
                    t = datetime.date.today() - datetime.timedelta(days=7)
                    epoch = int(datetime.datetime(year=t.year, month=t.month, day=t.day).timestamp())
                    db.connection.execute("DELETE FROM query WHERE to_timestamp(time) < to_timestamp(?)", [epoch])

                if len(self.buffer) > 0:
                    # construct a dataframe from the current buffer and empty it. This is orders of magniture
                    # faster than transactional inserts, and doesn't block even under high load.
                    db.connection.append('query', pandas.DataFrame(self.buffer))
                    self.buffer.clear()
                for client in self.update_clients:
                    # attempt to resolve every client IP we've seen in between intervals (if necessary)
                    if  (now - self.client_map.get(client, 0)) > 3600:
                        self.client_map[client] = now
                        host = self._resolve_ip(client)
                        try:
                            db.connection.execute("INSERT INTO client VALUES (?, ?)", [client, host])
                        except duckdb.ConstraintException:
                            db.connection.execute("UPDATE client SET hostname=? WHERE ipaddr=?", [host, client])

        return True

    def run_logger(self):
        # set a signal handler
        signal.signal(signal.SIGINT, self._sig)
        signal.signal(signal.SIGTERM, self._sig)

        self._setup_db()

        r_count = 0
        pipe_ready = False
        # give dnsbl_module.py some time to create a pipe
        while not pipe_ready:
            try:
                # open() will block until a query has been pushed down the fifo
                self.fd = open(self.target_pipe, 'r')
                pipe_ready = True
            except InterruptedError:
                self.close_logger()
            except OSError:
                r_count += 1
                if r_count > 10:
                    syslog.syslog(syslog.LOG_ERR, "Unable to open pipe. This is likely because Unbound isn't running.")
                    sys.exit(1)
                time.sleep(1)

        self.selector.register(self.fd.fileno(), selectors.EVENT_READ, self._read)

        while True:
            # select() will block until a file object is ready. To handle a SIGTERM we raise
            # an exception in the signal handler, see _sig() for further details. On an exception
            # events will return an empty list (see https://docs.python.org/3/library/selectors.html#selectors.BaseSelector.select)
            events = self.selector.select()
            if not events:
                self.close_logger()
            for key, mask in events:
                callback = key.data
                if not callback(key.fileobj, mask):
                    # unbound closed pipe
                    self.close_logger()

def run(pipe, flush_interval):
    r = DNSReader(pipe, flush_interval)
    try:
        r.run_logger()
    except InterruptedError:
        r.close_logger()
    except Exception:
        raise

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--pipe', help='named pipe file location', default='/var/unbound/data/dns_logger')
    parser.add_argument('--flush_interval', help='interval to flush to db', default=10)

    inputargs = parser.parse_args()

    syslog.openlog('unbound', facility=syslog.LOG_LOCAL4)

    syslog.syslog(syslog.LOG_NOTICE, 'Backgrounding unbound logging backend.')
    run(inputargs.pipe, inputargs.flush_interval)
