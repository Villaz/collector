#!/usr/bin/python
"""server interface stats for TSDB and ElasticSearch"""

import sys
import time
import re
import requests
import json
import socket
import os
import subprocess
from datetime import datetime
from optparse import OptionParser

url = "http://192.168.1.13:4242/api/put?details"
interval = 15  # seconds

def save_opentsdb(metric, timestamp, value, tags):
    petition = {
                "metric": metric,
                "timestamp": timestamp,
                "value": value,
                "tags": tags
            }
    requests.post(url, data=json.dumps(petition), timeout=10)

def save_elasticsearch(document):

    def create_index():
        doc = {
            "settings": {
                "number_of_shards": 1
            },
            "mappings": {
                "_default_": {
                    "_timestamp": {
                        "enabled": True,
                        "store": True
                    }
                }
            }
        }
        requests.post('http://192.168.1.13:9200/egi/', data= json.dumps(doc))
        print "EGI index created"
    if requests.head('http://192.168.1.13:9200/egi/').status_code == 404:
        create_index()
    requests.post('http://192.168.1.13:9200/egi/node/', data=json.dumps(document), timeout=10)
    print "created doc elastic"


def cpu_info(f_cpuinfo, ts):
    f_cpuinfo.seek(0)
    cpu = {}
    writes = 0
    for line in f_cpuinfo:
        petition = None
        if line.find('model name') >= 0:
            model_name = line[line.find(':')+2:len(line)-1]
            cpu['model_name'] = model_name
            writes += 1
            #save_opentsdb("host.cpu.model", ts, model_name, {"host":socket.gethostname()})
            continue
        if line.find('cpu cores') >= 0:
            cpu_cores = line[line.find(':')+2:len(line)-1]
            cpu['cores'] = int(cpu_cores)
            save_opentsdb("host.cpu.cores", ts, cpu_cores, {"host": socket.gethostname()})
            writes += 1
        if writes >= 2:
            break
    cpu['node'] = 1
    save_opentsdb("host.nodes", ts, 1, {"host": socket.gethostname()})
    return cpu


def proc_avg(f, ts):
    f.seek(0)
    line = f.read()
    args = line.split(" ")
    save_opentsdb("proc.loadavg.1min", ts, args[0], {"host": socket.gethostname()})
    save_opentsdb("proc.loadavg.5min", ts, args[1], {"host": socket.gethostname()})
    save_opentsdb("proc.loadavg.15min", ts, args[2], {"host": socket.gethostname()})
    return {
        '1min': float(args[0]),
        '5min': float(args[1]),
        '15min': float(args[2])
    }


def up_time(f, ts):
    time = {}
    f.seek(0)
    for line in f:
        m = re.match("(\S+)\s+(\S+)", line)
        if m:
            save_opentsdb("proc.uptime.total", ts, m.group(1), {"host": socket.gethostname()})
            save_opentsdb("proc.uptime.now", ts, m.group(2), {"host": socket.gethostname()})
            time['total'] = float(m.group(1))
            time['now'] = float(m.group(2))
    return time


def mem_info(f, ts):
    mem = {}
    f.seek(0)
    for line in f:
        m = re.match("(\w+):\s+(\d+)\s+(\w+)", line)
        if m:
            if m.group(3).lower() == 'kb':
                # convert from kB to B for easier graphing
                value = str(int(m.group(2)) * 1024)
            else:
                value = m.group(2)
            save_opentsdb( "proc.meminfo.%s" % m.group(1).lower(), ts, value, {"host": socket.gethostname()})
            mem[m.group(1).lower()] = float(value)
    return mem


def stat(f, ts):
    cpu = {}
    f.seek(0)
    for line in f:
        m = re.match("(\w+)\s+(.*)", line)
        if not m:
            continue
        if m.group(1).startswith("cpu"):
            cpu_m = re.match("cpu(\d+)", m.group(1))
            if cpu_m:
                metric_percpu = '.percpu'
                tags = cpu_m.group(1)
            else:
                metric_percpu = ''
                tags = None

            fields = m.group(2).split()
            cpu_types = ['user', 'nice', 'system', 'idle', 'iowait',
                    'irq', 'softirq', 'guest', 'guest_nice']

            cpu["cpu%s" % metric_percpu] = {}
            # We use zip to ignore fields that don't exist.
            for value, field_name in zip(fields, cpu_types):
                t = {"host": socket.gethostname()}
                if tags is not None:
                    t['cpu'] = tags
                save_opentsdb("proc.stat.cpu%s" % metric_percpu, ts, value, t)
                cpu["cpu%s" % metric_percpu][field_name]= float(value)
    return cpu


def network(ts):
    output = subprocess.check_output('nicstat -np', shell=True)
    lines = output.split('\n')
    network = {'in': 0.0, 'out':0.0}
    for line in lines:
        if line == '':
            continue
        attrs = line.split(':')
        network['in'] += float(attrs[2])
        network['out'] += float(attrs[3])
    save_opentsdb("proc.net.in", ts, network['in'])
    save_opentsdb("proc.net.out", ts, network['out'])
    return network


def parse_cmdline(argv):
    """Parses the command-line."""

    # get arguments
    parser = OptionParser(description='Manages collectors which gather '
                                       'data and report back.')
    parser.add_option('-P', '--pidfile', dest='pidfile',
                      default='/var/run/tcollector.pid',
                      metavar='FILE', help='Write our pidfile')
    parser.add_option('-D', '--daemonize', dest='daemonize', action='store_true',
                      default=False, help='Run as a background daemon.')
    return parser.parse_args(args=argv[1:])


def daemonize():
    """Performs the necessary dance to become a background daemon."""
    if os.fork():
        os._exit(0)
    os.chdir("/")
    os.umask(022)
    os.setsid()
    os.umask(0)
    if os.fork():
        os._exit(0)
    stdin = open(os.devnull)
    stdout = open(os.devnull, 'w')
    os.dup2(stdin.fileno(), 0)
    os.dup2(stdout.fileno(), 1)
    os.dup2(stdout.fileno(), 2)
    stdin.close()
    stdout.close()
    os.umask(022)
    for fd in xrange(3, 1024):
        try:
            os.close(fd)
        except OSError:  # This FD wasn't opened...
            pass         # ... ignore the exception.


def write_pid(pidfile):
    """Write our pid to a pidfile."""
    f = open(pidfile, "w")
    try:
        f.write(str(os.getpid()))
    finally:
        f.close()

def main(argv):

    options, args = parse_cmdline(argv)

    if options.daemonize:
        daemonize()

    if options.pidfile:
        write_pid(options.pidfile)

    f_cpuinfo = open("/proc/cpuinfo",'r')
    f_loadavg = open("/proc/loadavg", "r")
    f_uptime = open("/proc/uptime", "r")
    f_meminfo = open("/proc/meminfo", "r")
    f_stat = open("/proc/stat", "r")

    while True:
        document = {}
        ts = int(time.time())
        date = datetime.now()

        document['node'] = cpu_info(f_cpuinfo, ts)
        document['proc'] = proc_avg(f_loadavg, ts)
        document['time'] = up_time(f_uptime, ts)
        document['mem'] = mem_info(f_meminfo, ts)
        document['cpu'] = stat(f_stat, ts)
        document['host'] = socket.gethostname()
        document['@timestamp'] = date.strftime("%Y-%m-%dT%H:%M:%S.000z")
        document['network'] = network(ts)
        save_elasticsearch(document)
        time.sleep(interval)

        network()
        print "Ended loop"


if __name__ == "__main__":
    sys.exit(main(sys.argv))
