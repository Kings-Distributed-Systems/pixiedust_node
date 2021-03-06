import time
import json
import os, signal
import sys
import platform
import subprocess
import hashlib
from functools import partial
from threading import Thread, Event, Lock
from .ReadWriteLock import ReadWriteLock

import tempfile
import IPython
import pandas
import numpy as np
from pixiedust.display import display
from pixiedust.utils.environment import Environment
from pixiedust.utils.shellAccess import ShellAccess

doneNode = False
doneLock = ReadWriteLock()
numpyFiles = []



RESERVED = ['true', 'false','self','this','In','Out']

try:
    VARIABLE_TYPES = (str, int, float, bool, unicode, dict, list)
except:
    # Python 3 => no unicode type
    VARIABLE_TYPES = (str, int, float, bool, dict, list)

class VarWatcher(object):
    """
    this class watches for cell "post_execute" events. When one occurs, it examines
    the IPython shell for variables that have been set (only numbers and strings).
    New or changed variables are moved over to the JavaScript environment.
    """

    def __init__(self, ip, ps, n):
        self.shell = ip
        self.ps = ps
        self.n = n
        ip.events.register('post_execute', self.post_execute)
        self.clearCache()
        try:
            self._np_home = tempfile.gettempdir() +'/'
        except E as Exception:
            print("Did not succeed in finding temp directory because: ", E)

    def setHome(self):
        self.n.write("var _np_home = '" + self._np_home + "';\r\n");

    def clearCache(self):
        self.cache = {}

    # the cache contains the key (variable name) against an MD5 of the
    # JSON form of the value. This makes the cache more compact.
    def setCache(self, key, val):
        self.cache[key] = hashlib.md5(json.dumps(val).encode("utf8")).hexdigest()

    # check whether key is in cache and whether it equals val by comparing
    # the hash of its JSON value with what's in the cache
    def inCache(self, key, val):
        hash = hashlib.md5(json.dumps(val).encode("utf8")).hexdigest()
        return (key in self.cache and self.cache[key] == hash)

    def post_execute(self):
        for key in self.shell.user_ns:
            v = self.shell.user_ns[key]
            t = type(v)
            # if this is one of our variables, is a number or a string or a float
            if not key.startswith('_') and (not key in RESERVED) and (t in VARIABLE_TYPES):
                # if it's not in our cache or it is an its value has changed
                if not key in self.cache or not self.inCache(key, v):
                    # move it to JavaScript land and add it to our cache
                    dumped = json.dumps(v)
                    self.n.write("var " + key + " = " + dumped + ";\r\n")
                    self.setCache(key, v)
            elif not key.startswith('_') and (not key in RESERVED) and (t == np.ndarray) and (v.size > 0):
                np_hash = str(v.data.tobytes()) + str(v.shape)
                if not key in self.cache or not self.inCache(key, np_hash):
                    loc = self._np_home + key + '.npy'
                    if os.path.exists(loc):
                        os.remove(loc)
                    np.save(loc, v)
                    self.n.write("var " + key + " = readNumpyFile( '" + loc + "' );\r\n");
                    self.setCache(key, np_hash);
                    global numpyFiles
                    numpyFiles.append(loc)

class NodeStdReader(Thread):
    """
    Thread class that is given a process in the constructor
    the thead listens to each line coming out of the
    process's stdout and checks to see if it is JSON.
    if it is, and it's a special Pixiedust command,
    then the pixiedust display/print function is called
    """

    def __init__(self, ps, vw):
        super(NodeStdReader, self).__init__()
        self._stop_event = Event()
        self.ps = ps
        self.vw = vw
        self.daemon = True
        self.start()

    def stop(self):
        self._stop_event.set()

    def run(self):
        # forever
        while not self._stop_event.is_set():
            # read line from Node's stdout
            line = self.ps.stdout.readline()
            # see if it parses as JSON
            obj = None
            try:
                if line:
                    obj = json.loads(line)
                    # if it does and is a pixiedust object
                    if obj and isinstance(obj, dict) and obj['_pixiedust']:
                        if obj['type'] == 'display':
                            pdf = pandas.DataFrame(obj['data'])
                            ShellAccess.pdf = pdf
                            display(pdf)
                        elif obj['type'] == 'print':
                            print(json.dumps(obj['data']))
                        elif obj['type'] == 'store':
                            print('!!! Warning: store is now deprecated - Node.js global variables are automatically propagated to Python !!!')
                            variable = 'pdf'
                            if 'variable' in obj:
                                variable = obj['variable']
                            ShellAccess[variable] = pandas.DataFrame(obj['data'])
                        elif obj['type'] == 'html':
                            IPython.display.display(IPython.display.HTML(obj['data']))
                        elif obj['type'] == 'image':
                            IPython.display.display(IPython.display.HTML('<img src="{0}" />'.format(obj['data'])))
                        elif obj['type'] == 'variable':
                            ShellAccess[obj['key']] = obj['value']
                            if self.vw:
                                self.vw.setCache(obj['key'], obj['value'])
                        elif obj['type'] == 'numpy':
                            loc = obj['data']
                            key = os.path.splitext(os.path.basename(loc))[0]
                            #print(key , "has been overriden")
                            v = np.load(loc)
                            ShellAccess[key] = v
                            if self.vw:
                                np_hash = str(v.data.tobytes()) + str(v.shape)
                                self.vw.setCache(key, np_hash)
                        elif obj['type'] == 'done':
                            global doneNode
                            global doneLock
                            doneLock.acquire_write()
                            doneNode = True
                            doneLock.release_write()
                    else:
                        print(line)
            except Exception as e:
                # output the original line when we don't have JSON
                test_line = line.strip().replace(' ', '')
                if len(test_line) > 0:
                    print(line.strip())



class NodeBase(object):
    """
    Node base class with common tasks for Node.js and NPM process runs.
    """
    @staticmethod
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    @staticmethod
    def which(program):
        fpath, fname = os.path.split(program)
        if fpath:
            if NodeBase.is_exe(program):
                return program
        else:
            for path in os.environ["PATH"].split(os.pathsep):
                path = path.strip('"')
                exe_file = os.path.join(path, program)
                if NodeBase.is_exe(exe_file):
                    return exe_file

        return None

    def __init__(self):
        """
        Establishes the Node's home directories and executable paths
        """
        # get the home directory
        home = Environment.pixiedustHome

        # Node home directory
        self.node_home = os.path.join(home, 'node')
        if not os.path.exists(self.node_home):
            os.makedirs(self.node_home)

        # Node modules home directory
        self.node_modules = os.path.join(self.node_home, 'node_modules')
        if not os.path.exists(self.node_modules):
            os.makedirs(self.node_modules)

        self.node_prog = 'node'
        self.npm_prog = 'npm'
        if platform.system() == 'Windows':
            self.node_prog += '.exe'
            self.npm_prog += '.cmd'

        self.node_path = NodeBase.which(self.node_prog)
        if self.node_path is None:
            print('ERROR: Cannot find Node.js executable')
            raise FileNotFoundError('node executable not found in path')

        self.npm_path = NodeBase.which(self.npm_prog)
        if self.npm_path is None:
            print('ERROR: Cannot find npm executable')
            raise FileNotFoundError('npm executable not found in path')

        # Create popen partial, that will be used later
        popen_kwargs = {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.STDOUT,
            'cwd': self.node_home
        }
        if sys.version_info.major == 3:
            popen_kwargs['encoding'] = 'utf-8'
        self.popen = partial(subprocess.Popen, **popen_kwargs)


class Node(NodeBase):
    """
    Class runs a Node sub-process and starts a NodeStdReader thread
    to listen to its stdout.
    """
    def __init__(self, path):
        """
        Constructor runs a JavaScript script (path) with "node"
        :param path: JavaScript path
        """
        super(Node, self).__init__()
        self._path = path
        # process that runs the Node.js code  
        args = (self.node_path,'--experimental-repl-await', '--max-old-space-size=10000', path)
        self.ps = self.popen(args)
        self.psid = self.ps.pid
        #print ("Node process id", self.ps.pid)

        # watch Python variables for changes
        self.vw = VarWatcher(get_ipython(), self.ps, self)

        # create thread to read this process's output
        self.nsr = NodeStdReader(self.ps, self.vw)

        self.vw.setHome()

    def terminate(self):
        global numpyFiles
        for i in range(len(numpyFiles)):
            if os.path.exists(numpyFiles[i]):
                os.remove(numpyFiles[i])
        numpyFiles = []
        self.ps.terminate()

    def write(self, s):
        global doneNode
        global doneLock
        doneLock.acquire_write()
        doneNode = False
        doneLock.release_write()
        try:
            self.ps.stdin.write(s)
            self.ps.stdin.write("\r\n")
            self.ps.stdin.flush()
        except Exception as e:
            #if our pipe is broken, reinitialize everything
            if 'Broken pipe' in str(e):
                self.nsr.stop()
                os.kill(self.psid, signal.SIGKILL)
                self.__init__(self._path)
                doneLock.acquire_write()
                doneNode = True
                doneLock.release_write()
                self.write( "let { parseNumpyFile, unparseNumpyFile, readNumpyFile, writeNumpyFile, buildDataArray } = require('npy-js');")
                sys.stdout.flush()
                return
            else:
                doneLock.acquire_write()
                doneNode = True
                doneLock.release_write()
                return
        while True:
            flag = False
            doneLock.acquire_read()
            flag = doneNode
            doneLock.release_read()
            if flag:
                break

    def cancel(self):
        self.write("\r\n.break")

    def clear(self):
        self.write("\r\n.clear")
        self.vw.clearCache()
        #reset home location and reset np variables
        self.vw.setHome()
        self.write( "let { parseNumpyFile, unparseNumpyFile, readNumpyFile, writeNumpyFile, buildDataArray } = require('npy-js');")

    def help(self):
        self.cancel()
        self.write("help()\r\n")


class Npm(NodeBase):
    """
    npm helper class
    allows npm modules to be installed, removed and listed
    """
    def __init__(self):
        super(Npm, self).__init__()

    # run an npm command
    def cmd(self, command, module):
        args = [self.npm_path, command, '-s']
        if module:
            if isinstance(module, str):
                args.append(module)
            else:
                args.extend(module)
        print(' '.join(args))
        ps = self.popen(args)

        # create thread to read this process's output
        t = NodeStdReader(ps, None)

        # wait for the sub-process to exit
        ps.wait()

        # tell the thread reading its output to stop too, to prevent 100% CPU usage
        t.stop()

    def install(self, module):
        self.cmd('install', module)

    def remove(self, module):
        self.cmd('uninstall', module)

    def uninstall(self, module):
        self.cmd('uninstall', module)

    def list(self):
        self.cmd('list', None)
