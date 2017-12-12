import os
import re

import subprocess
from Queue import Queue, Empty
from threading import Thread

from celery import Celery
from celery.contrib import rdb

from ovirt_imageio_common import directio

app = Celery('celery_tasks', backend='rpc://', broker='amqp://localhost//')
#rdb.set_trace()

def is_blk_device(dev):
    try:
        if stat.S_ISBLK(os.stat(dev).st_mode):
            return True
        return False
    except Exception:
        print ('Path %s not found in is_blk_device check', dev)
        return False


def check_for_odirect_support(src, dest, flag='oflag=direct'):

    # Check whether O_DIRECT is supported
    try:
        nova_utils.execute('dd', 'count=0', 'if=%s' % src, 'of=%s' % dest,
                           flag, run_as_root=True)
        return True
    except processutils.ProcessExecutionError:
        return False


def enqueue_output(out, queue):
    line = out.read(17)
    while line:
        line = out.read(17)
        queue.put(line)
    out.close()

@app.task(bind=True)
def backup(self, ticket_id, path, dest, size, buffer_size):
    op = directio.Send(path,
                      None,
                      size,
                      buffersize=buffer_size)
    total = 0
    print('Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}'.format(
            self.request))
    gigs = 0
    with open(dest, "w+") as f:
        #rdb.set_trace()
        for data in op:
            total += len(data)
            f.write(data)
            if total/1024/1024/1024 > gigs:
                gigs = total/1024/1024/1024
                self.update_state(state='PENDING',
                                  meta={'size': size, 'total': total})


@app.task(bind=True)
def restore(self, ticket_id, volume_path, backup_image_file_path, size, buffer_size):

    def transfer_qemu_image_to_volume(
            volume_path,
            backup_image_file_path):

        cmdspec = [
            'qemu-img',
            'convert',
            '-p',
        ]

        if is_blk_device(volume_path) and \
            check_for_odirect_support(backup_image_file_path,
                                      volume_path, flag='oflag=direct'):
            cmdspec += ['-t', 'none']

        cmdspec += ['-O', 'raw', backup_image_file_path, volume_path]

        default_cache = True
        if default_cache is True:
            if '-t' in cmdspec:
                cmdspec.remove('-t')
            if 'none' in cmdspec:
                cmdspec.remove('none')
        cmd = " ".join(cmdspec)
        print('transfer_qemu_image_to_volume cmd %s ' % cmd)
        process = subprocess.Popen(cmdspec,
                                   stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   bufsize=-1,
                                   close_fds=True,
                                   shell=False)

        queue = Queue()
        read_thread = Thread(target=enqueue_output,
                             args=(process.stdout, queue))

        read_thread.daemon = True  # thread dies with the program
        read_thread.start()

        percentage = 0.0
        while process.poll() is None:
            try:
                try:
                    output = queue.get(timeout=300)
                except Empty:
                    continue
                except Exception as ex:
                    print(ex)

                percentage = re.search(r'\d+\.\d+', output).group(0)

                print(("copying from %(backup_path)s to "
                           "%(volume_path)s %(percentage)s %% completed\n") %
                          {'backup_path': backup_image_file_path,
                           'volume_path': volume_path,
                           'percentage': str(percentage)})

                percentage = float(percentage)

                self.update_state(state='PENDING',
                                  meta={'percentage': percentage})

            except Exception as ex:
                pass

        process.stdin.close()

        _returncode = process.returncode  # pylint: disable=E1101
        if _returncode:
            print(('Result was %s' % _returncode))
            raise Exception("Execution error %(exit_code)d (%(stderr)s). "
                            "cmd %(cmd)s" %
                            {'exit_code': _returncode,
                             'stderr': process.stderr.read(),
                             'cmd': cmd})

    transfer_qemu_image_to_volume(volume_path, backup_image_file_path)
