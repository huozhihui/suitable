from ansible.executor.task_queue_manager import TaskQueueManager
from ansible.inventory import Inventory
from ansible.parsing.dataloader import DataLoader
from ansible.playbook.play import Play
from ansible.vars import VariableManager
from datetime import datetime
from pprint import pformat
from suitable.callback import SilentCallbackModule
from suitable.common import log
from suitable.runner_results import RunnerResults


class ModuleRunner(object):

    def __init__(self, module_name):
        """ Runs any ansible module given the module's name and access
        to the api instance (done through the hookup method).

        """
        self.module_name = module_name
        self.api = None
        self.module_args = None

    def __str__(self):
        """ Return a represenation of the module, including the last
        run module_args (-> this will end up looking a lot like) an entry
        in an ansible yaml file.

        """
        return "{}: {}".format(self.module_name, self.module_args)

    @property
    def is_hooked_up(self):
        return self.api is not None and hasattr(self.api, self.module_name)

    def hookup(self, api):
        """ Hooks this module up to the given api. """

        assert not hasattr(api, self.module_name), """
            '{}' conflicts with existing attribute
        """.format(self.module_name)

        self.api = api

        setattr(api, self.module_name, self.execute)

    def get_module_args(self, args, kwargs):
        args = u' '.join(args)

        kwargs = u' '.join(u'{}="{}"'.format(
            k, v.replace('"', '\\"')) for k, v in kwargs.items())

        return u' '.join((args, kwargs)).strip()

    def execute(self, *args, **kwargs):
        """ Puts args and kwargs in a way ansible can understand. Calls ansible
        and interprets the result.

        """

        assert self.is_hooked_up, "the module should be hooked up to the api"

        self.module_args = module_args = self.get_module_args(args, kwargs)

        loader = DataLoader()
        variable_manager = VariableManager()
        inventory = Inventory(
            loader=loader,
            variable_manager=variable_manager,
            host_list=self.api.servers
        )
        variable_manager.set_inventory(inventory)

        play_source = {
            'name': "Suitable Play",
            'hosts': self.api.servers,
            'gather_facts': 'no',
            'tasks': [{
                'action': {
                    'module': self.module_name,
                    'args': module_args
                }
            }]
        }

        play = Play().load(
            play_source,
            variable_manager=variable_manager,
            loader=loader
        )

        log.info(u'running {}'.format(u'- {module_name}: {module_args}'.format(
            module_name=self.module_name,
            module_args=module_args
        )))

        start = datetime.utcnow()
        task_queue_manager = None
        callback = SilentCallbackModule()

        try:
            task_queue_manager = TaskQueueManager(
                inventory=inventory,
                variable_manager=variable_manager,
                loader=loader,
                options=self.api.options,
                passwords={},
                stdout_callback=callback
            )
            task_queue_manager.run(play)
        finally:
            if task_queue_manager is not None:
                task_queue_manager.cleanup()

        log.info(u'took {} to complete'.format(datetime.utcnow() - start))

        return self.evaluate_results(callback)

    def ignore_further_calls_to_server(self, server):
        """ Takes a server out of the list. """
        log.error(u'ignoring further calls to {}'.format(server))
        self.api.servers.remove(server)

    def trigger_event(self, server, method, args):
        try:
            action = getattr(self.api, method)(*args)

            if action != 'keep-trying':
                self.ignore_further_calls_to_server(server)
        except:
            self.ignore_further_calls_to_server(server)
            raise

    def evaluate_results(self, callback):
        """ prepare the result of runner call for use with RunnerResults. """

        for server, result in callback.unreachable.items():
            log.error(u'{} could not be reached'.format(server))
            log.debug(u'ansible-output =>\n{}'.format(pformat(result)))

            if self.api.ignore_unreachable:
                continue

            self.trigger_event(server, 'on_unreachable_host', (
                self, server
            ))

        for server, answer in callback.contacted.items():

            success = answer['success']
            result = answer['result']

            if 'failed' in result:
                success = False

            if 'rc' in result:
                if self.api.is_valid_return_code(result['rc']):
                    success = True

            if not success:
                log.error(u'{} failed on {}'.format(self, server))
                log.debug(u'ansible-output =>\n{}'.format(pformat(result)))

                if self.api.ignore_errors:
                    continue

                self.trigger_event(server, 'on_module_error', (
                    self, server, result
                ))

        # XXX this is a weird structure because RunnerResults still works
        # like it did with Ansible 1.x, where the results where structured
        # like this
        return RunnerResults({
            'contacted': {
                server: answer['result']
                for server, answer in callback.contacted.items()
            }
        })
