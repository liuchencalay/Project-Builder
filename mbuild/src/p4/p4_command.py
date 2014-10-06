import os
import sys
import subprocess
import re

"""This is wrapper around a 'p4' command line binary. It seems that having
the P4Python module installed is often a pain, and it doesn't seem to be
particularly helpful either."""

BASE = os.path.join(os.path.dirname(__file__), '..', '..', '..')
sys.path.append(os.path.join(BASE, 'mbuild'))

from src.util.which import which

class NoP4BinaryError(Exception): pass
class P4Error(Exception): pass

def _view_escape(s):
	"""This escapes a depot path so it can be included in a view spec."""
	bad_chars = '@#*% '
	if not any([c in bad_chars for c in s]):
		# No difficult characters
		return s
	
	escaped = ''
	for c in s:
		if c in bad_chars:
			escaped += '%' + hex(ord(c))[2:]
		else:
			escaped += c
	
	return escaped


class P4Command(object):
	def __init__(self, port, user, password, workspace, loud):
		assert isinstance(port, str)
		assert isinstance(user, str)
		assert isinstance(password, str)
		assert isinstance(workspace, str)
		assert isinstance(loud, bool)
		
		self._bin = which('p4')
		if self._bin is None:
			raise NoP4BinaryError()
		
		self._user = user
		self._workspace = workspace
		
		self._run_env = dict(os.environ)
		self._run_env['P4USER'] = user
		self._run_env['P4PASSWD'] = password
		self._run_env['P4PORT'] = port
		self._run_env['P4CLIENT'] = workspace
		
		self._changelist_number = None
		self._loud = loud
	
	def add(self, changelist, path):
		assert isinstance(changelist, str)
		assert isinstance(path, str)
		
		self._p4(['add', '-c', changelist, '-I', path], None)
	
	def edit(self, changelist, path):
		assert isinstance(path, str)
		
		self._p4(['edit', '-c', changelist, path], None)
	
	def reopen(self, changelist, filetype, path):
		assert isinstance(changelist, str)
		assert isinstance(filetype, str)
		assert isinstance(path, str)
		
		self._p4(['reopen', '-c', changelist, '-t', filetype, path], None)
	
	def revert(self, path):
		assert isinstance(path, str)
		
		self._p4(['revert', path], None)
	
	def treerevert(self, path):
		assert isinstance(path, str)
		print "treerevert(%s)" % path
		to_revert = []
		for p, d, files in os.walk(self.where(path)['client_fs']):
			to_revert += [os.path.join(p, f) for f in files]
			
		self._p4(['revert'] + to_revert, None)
	
	def merge_changelists(self, keep, merge):
		assert isinstance(keep, str)
		assert all([c.isdigit() for c in keep])
		assert isinstance(merge, str)
		assert all([c.isdigit() for c in merge])
		
		# Get the list of files in the 'merge' changelist
		opened, stderr, returncode = self._p4(['opened', '-c', merge], None)
		if returncode != 0:
			raise P4Error("Couldn't get files open in changelist %s" % merge)
		
		files = []
		for line in opened.splitlines():
			files.append(line.partition('#')[0])
		
		# Move files to 'keep' changelist
		if len(files) > 0:
			stdout, stderr, returncode = self._p4(['reopen', '-c', keep] + files, None)
			if returncode != 0:
				raise P4Error("Couldn't reopen %d files including %s: %s" % (len(files), files[0], stderr))
		
		# Delete 'merge' changelist
		stdout, stderr, returncode = self._p4(['changelist', '-d', merge], None)
		if returncode != 0:
			raise P4Error("Couldn't delete changelist %s: %s" % (merge, stderr))
	
	def copy(self, changelist, source, dest):
		assert isinstance(source, str)
		assert isinstance(dest, str)
		
		self._p4(['copy', '-c', changelist, source, dest], None)
	
	def bulk_copy(self, changelist, copies):
		assert isinstance(copies, list)
		assert all([len(c) == 2 for c in copies])
		assert all([isinstance(src, str) and isinstance(dest, str) for src, dest in copies])
		assert all([src.startswith('//') and dest.startswith('//') for src, dest in copies])
		
		# When doing multiple copies, we can speed this up by creating a
		# temporary branch spec, adding our copies to it, running the branch
		# spec, and then deleting it.
		branch_name = '%s-%s-temp-%s' % (self._user, self._workspace, changelist)
		
		
		view = ''.join(['\t%s %s\n' % (_view_escape(src), _view_escape(dest)) for src, dest in copies])
		new_branch_spec = \
"""Branch: %s

Owner: %s

Description:
	Temporary branch spec generated by M-Build. Should be able to be safely be deleted if you aren't running any M-Build perforce commands.

Options: unlocked

View:
%s
""" % (branch_name, self._user, view)

		# Create temporary branch spec
		out, err, returncode = self._p4(['branch', '-i'], new_branch_spec)
		if returncode != 0:
			raise P4Error("Couldn't create temporary branch '%s': %s" % (branch_name, err))
		try:
			# Do copy
			out, err, returncode = self._p4(['copy', '-c', changelist, '-b', branch_name], None)
			if returncode != 0:
				raise P4Error("Couldn't copy with branch '%s': %s" % (branch_name, err))
		finally:
			# Delete temporary branch spec
			out, err, returncode = self._p4(['branch', '-d', branch_name], None)
			if returncode != 0:
				raise P4Error("Couldn't delete temporary branch '%s': %s" % (branch_name, err))
	
	def _p4(self, args, stdin):
		"""Returns (stdout, stderr, returncode)"""
		cmd = [self._bin] + args
		proc = subprocess.Popen(cmd,
		                        stdin=subprocess.PIPE,
		                        stdout=subprocess.PIPE,
		                        stderr=subprocess.PIPE,
		                        env=self._run_env)
		
		if self._loud:
			print ' '.join(cmd)
		
		stdout, stderr = proc.communicate(stdin)
		
		return (stdout, stderr, proc.returncode)
	
	def where(self, path):
		out, err, returncode = self._p4(['where', path], None)
		if returncode != 0:
			raise P4Error("Couldn't find '%s': %s" % (path, err))
		depot_name, client_name_perforce, client_name_local = out.split(' ')
		return {'depot': depot_name,
		        'client_p4': client_name_perforce,
		        'client_fs': client_name_local.rstrip('\n')} # drop newline
	
	def get_changelist_number(self):
		# p4 change -i
		new_changelist_spec = \
"""Change: new

Client: %s

User: %s

Status: new

Description:
\tM-Build generated changelist.

""" % (self._workspace, self._user)
		
		out, err, returncode = self._p4(['change', '-i'], new_changelist_spec)
		if returncode != 0:
			raise p4Error("Couldn't create new changelist: %s" % err)
		
		result = re.match('Change ([0-9]+) created.', out)
		if not result:
				raise P4Error("Couldn't determine changelist number. p4 stdout was '%s' (stderr was '%s')" % (out, err))
		return result.group(1)
