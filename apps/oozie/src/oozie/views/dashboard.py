#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from desktop.log.access import access_warn

try:
  import json
except ImportError:
  import simplejson as json
import logging

from django.http import HttpResponse
from django.utils.functional import wraps
from django.utils.translation import ugettext as _

from desktop.lib.django_util import render, PopupException
from desktop.lib.rest.http_client import RestException
from liboozie.oozie_api import get_oozie

from oozie.models import History
from oozie.views.editor import can_access_job


LOG = logging.getLogger(__name__)


"""
Permissions:

A Workflow/Coordinator can be accessed/submitted/modified only by its owner or a superuser.

Permissions checking happens by calling check_access_and_get_oozie_job().
"""


def show_oozie_error(view_func):
  def decorate(request, *args, **kwargs):
    try:
      return view_func(request, *args, **kwargs)
    except RestException, ex:
      raise PopupException(_('Sorry, an error with Oozie happened.'), detail=ex._headers.get('oozie-error-message', ex))
  return wraps(view_func)(decorate)


def manage_oozie_jobs(request, job_id, action):
  if request.method != 'POST':
    raise PopupException(_('Please use a POST request to manage an Oozie job.'))

  check_access_and_get_oozie_job(request, job_id)

  response = {'status': -1, 'data': ''}

  try:
    response['data'] = get_oozie().job_control(job_id, action)
    response['status'] = 0
    request.info(_('Action %(action)s was performed on job %(job_id)s') % {'action': action, 'job_id': job_id})
  except RestException, ex:
    response['data'] = _("Error performing %s on Oozie job %s: %s") % (action, job_id, ex.message)

  return HttpResponse(json.dumps(response), mimetype="application/json")


@show_oozie_error
def list_oozie_workflows(request):
  kwargs = {'cnt': 50,}
  if not request.user.is_superuser:
    kwargs['user'] = request.user.username

  workflows = get_oozie().get_workflows(**kwargs)

  return render('dashboard/list_oozie_workflows.mako', request, {
    'user': request.user,
    'jobs': split_oozie_jobs(workflows.jobs),
  })


@show_oozie_error
def list_oozie_coordinators(request):
  kwargs = {'cnt': 50,}
  if not request.user.is_superuser:
    kwargs['user'] = request.user.username

  coordinators = get_oozie().get_coordinators(**kwargs)

  return render('dashboard/list_oozie_coordinators.mako', request, {
    'jobs': split_oozie_jobs(coordinators.jobs),
  })


@show_oozie_error
def list_oozie_workflow(request, job_id, coordinator_job_id=None):
  oozie_workflow = check_access_and_get_oozie_job(request, job_id)

  oozie_coordinator = None
  if coordinator_job_id is not None:
    oozie_coordinator = check_access_and_get_oozie_job(request, coordinator_job_id)

  history = History.cross_reference_submission_history(request.user, job_id, coordinator_job_id)

  hue_coord = history and history.get_coordinator() or History.get_coordinator_from_config(oozie_workflow.conf_dict)
  hue_workflow = (hue_coord and hue_coord.workflow) or (history and history.get_workflow()) or History.get_workflow_from_config(oozie_workflow.conf_dict)

  if hue_coord: can_access_job(request, hue_coord.workflow.id)
  if hue_workflow: can_access_job(request, hue_workflow.id)

  # Add parameters from coordinator to workflow if possible
  parameters = {}
  if history and history.properties_dict:
    parameters = history.properties_dict
  elif hue_workflow is not None:
    for param in hue_workflow.find_parameters():
      if param in oozie_workflow.conf_dict:
        parameters[param] = oozie_workflow.conf_dict[param]


  return render('dashboard/list_oozie_workflow.mako', request, {
    'history': history,
    'oozie_workflow': oozie_workflow,
    'oozie_coordinator': oozie_coordinator,
    'hue_workflow': hue_workflow,
    'hue_coord': hue_coord,
    'parameters': parameters,
  })


@show_oozie_error
def list_oozie_coordinator(request, job_id):
  oozie_coordinator = check_access_and_get_oozie_job(request, job_id)

  # Cross reference the submission history (if any)
  coordinator = None
  try:
    coordinator = History.objects.get(oozie_job_id=job_id).job.get_full_node()
  except History.DoesNotExist:
    pass

  return render('dashboard/list_oozie_coordinator.mako', request, {
    'oozie_coordinator': oozie_coordinator,
    'coordinator': coordinator,
  })


@show_oozie_error
def list_oozie_workflow_action(request, action):
  try:
    action = get_oozie().get_action(action)
    workflow = check_access_and_get_oozie_job(request, action.id.split('@')[0])
  except RestException, ex:
    raise PopupException(_("Error accessing Oozie action %s") % (action,),
                         detail=ex.message)

  return render('dashboard/list_oozie_workflow_action.mako', request, {
    'action': action,
    'workflow': workflow,
  })


def split_oozie_jobs(oozie_jobs):
  jobs = {}
  jobs_running = []
  jobs_completed = []

  for job in oozie_jobs:
    if job.status == 'RUNNING':
      jobs_running.append(job)
    else:
      jobs_completed.append(job)

  jobs['running_jobs'] = sorted(jobs_running, key=lambda w: w.status)
  jobs['completed_jobs'] = sorted(jobs_completed, key=lambda w: w.status)

  return jobs


def check_access_and_get_oozie_job(request, job_id):
  """
  Decorator ensuring that the user has access to the workflow or coordinator.

  Arg: 'workflow' or 'coordinator' oozie id.
  Return: the Oozie workflow of coordinator or raise an exception

  Notice: its gets an id in input and returns the full object in output (not an id).
  """
  if job_id is not None:
    if job_id.endswith('W'):
      get_job = get_oozie().get_job
    else:
      get_job = get_oozie().get_coordinator

    try:
      oozie_job = get_job(job_id)
    except RestException, ex:
      raise PopupException(_("Error accessing Oozie job %s") % (job_id,),
                           detail=ex._headers['oozie-error-message'])

  if request.user.is_superuser or oozie_job.user == request.user.username:
    return oozie_job
  else:
    message = _("Permission denied. %(username)s don't have the permissions to access job %(id)s") % \
        {'username': request.user.username, 'id': oozie_job.id}
    access_warn(request, message)
    raise PopupException(message)