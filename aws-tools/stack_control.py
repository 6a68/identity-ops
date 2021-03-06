import logging
logging.basicConfig(level=logging.DEBUG)

import json
import time
import os
import itertools

def create_stack(region, environment, stack_type, availability_zones, path, replace=False, name=None, key_name=None, mini_stack=False):
    if name == None:
        # Maybe we set the stack name to the username of the user creating with a number suffix?
        import random
        name = str(random.randint(1,9999))
    if len(name) > 4:
        raise ValueError("name must not exceed 4 characters in length. '%s' is too long" % name)

    if not key_name:
        key_name = '20130416-svcops-base-key'

    from string import Template
    import boto.ec2
    import boto.ec2.elb
    import boto.ec2.elb.healthcheck
    import boto.vpc
    import boto.iam
    import boto.ec2.cloudwatch
    import boto.ec2.cloudwatch.alarm
    conn_iam = boto.iam.connect_to_region('universal')
    conn_elb = boto.ec2.elb.connect_to_region(region)
    conn_vpc = boto.vpc.connect_to_region(region)
    conn_ec2 = boto.ec2.connect_to_region(region)
    conn_cw = boto.ec2.cloudwatch.connect_to_region(region)
    stack = {}

    ami_map = json.load(open('config/ami_map.json', 'r'))

    existing_vpcs = conn_vpc.get_all_vpcs()
    # This will throw an IndexError exception if the VPC isn't found which isn't very intuitive
    vpc = [x for x in existing_vpcs if 'Name' in x.tags and x.tags['Name'] == environment][0]
    
    stack['loadbalancer'] = []
    existing_load_balancers = conn_elb.get_all_load_balancers()
    existing_security_groups = conn_ec2.get_all_security_groups()
    existing_certs = conn_iam.get_all_server_certs(path_prefix=path)['list_server_certificates_response']['list_server_certificates_result']['server_certificate_metadata_list']
    existing_subnets = conn_vpc.get_all_subnets(filters=[('vpcId', [vpc.id])])

    for load_balancers_params in json.load(open('config/elbs_public.%s.json' % stack_type, 'r')) + json.load(open('config/elbs_private.json')):
        load_balancers_params['name'] = '%s-%s' % (load_balancers_params['name'], name)
        for listener in load_balancers_params['listeners']:
            if len(listener) == 4:
                # Convert the cert name to an ARN
                #listener[3] = global_data['certs'][listener[3]]['arn']
                # This thows an IndexError if it can't find the cert which isn't intuitive
                try:
                    listener[3] = [x.arn for x in existing_certs if x['server_certificate_name'] == listener[3]][0]
                except IndexError:
                    logging.error("unable to find cert %s in certs %s" % (listener[3], existing_certs))
                    raise

        #subnets = []
        #for availability_zone in vpc['availability_zones'].keys():
        #    for subnet_name in load_balancers_params['subnets']:
        #        subnets.append(vpc['availability_zones'][availability_zone]['subnets'][subnet_name].id)

        subnets = [x for x in existing_subnets if 'Name' in x.tags and environment + '-' + load_balancers_params['subnet'] in x.tags['Name']]

        security_groups = [x for x in existing_security_groups if x.name in [environment + '-' + y for y in load_balancers_params['security_groups']]]

        # This doesn't converge the configuration of the loadbalancer
        # it merely checks if it exists
        if load_balancers_params['name'] in [x.name for x in existing_load_balancers]:
            if replace:
                conn_elb.delete_load_balancer(load_balancers_params['name'])
            else:
                continue

        stack['loadbalancer'].append(conn_elb.create_load_balancer(
                                       name=load_balancers_params['name'], 
                                       zones=None,
                                       listeners=load_balancers_params['listeners'],
                                       subnets=[x.id for x in subnets],
                                       security_groups=[x.id for x in security_groups],
                                       scheme='internal' if load_balancers_params['is_internal'] else 'internet-facing'
                                       ))
        load_balancer = stack['loadbalancer'][-1]
        
        # TODO : tag the load_balancer

        healthcheck_params = load_balancers_params['healthcheck'] if 'healthcheck' in load_balancers_params else {
            "interval" : 30,
            "target" : "HTTP:80/__heartbeat__",
            "healthy_threshold" : 3,
            "timeout" : 5,
            "unhealthy_threshold" : 5
        }
        # healthcheck_params['access_point'] = load_balancer[name]
        load_balancer.configure_health_check(boto.ec2.elb.healthcheck.HealthCheck(**healthcheck_params))

        # monitor the ELB
        metric = "HTTPCode_Backend_5XX"
        threshold = 0.1
        period = 120
        metric_alarm = boto.ec2.cloudwatch.alarm.MetricAlarm(
            name="%s %s" % (load_balancers_params['name'], metric),
            metric=metric,
            namespace="AWS/ELB",
            statistic="Average",
            comparison=">=",
            threshold=threshold,
            period=period,
            evaluation_periods=1,
            unit="Count",
            alarm_actions=["arn:aws:sns:us-west-2:351644144250:identity-alert"],
            dimensions={"LoadBalancerName": load_balancers_params['name']},
            description="Alarm when the rate of %s exceeds the threshold %s for %s seconds on the %s ELB" % (
                         metric, threshold, period, load_balancers_params['name']))
        conn_cw.put_metric_alarm(metric_alarm)
        
        stack['loadbalancer'].append(load_balancer)

    existing_load_balancers = conn_elb.get_all_load_balancers()

    stack_info = {}
    stack_info['load_balancers'] = {}
    for x in [y for y in existing_load_balancers if y.vpc_id == vpc.id and y.name.endswith('-%s' % name)]:
        stack_info['load_balancers'][x.name[:-len('-%s' % name)]] = {}
        stack_info['load_balancers'][x.name[:-len('-%s' % name)]]['dns_name'] = x.dns_name
        stack_info['load_balancers'][x.name[:-len('-%s' % name)]]['name'] = x.name
    # TODO add in -univ devices
    stack_info.update({'name': name,
                       'type': stack_type,
                       'environment': environment})
    
    # auto scale
    import boto.ec2.autoscale
    import boto.ec2.autoscale.tag
    conn_autoscale = boto.ec2.autoscale.connect_to_region(region)

    stack['launch_configuration'] = []
    stack['autoscale_group'] = []

    # I'm going to combine launch configuration and autoscale group because I don't
    # see us having more than one autoscale group for each launch configuration

    for autoscale_params in json.load(open('config/autoscale.%s.json' % stack_type, 'r')):
        launch_configuration_params = autoscale_params['launch_configuration']

        if 'AWS_CONFIG_DIR' in os.environ:
            user_data_filename = os.path.join(os.environ['AWS_CONFIG_DIR'], 'userdata.%s.%s.json' % (stack_type, launch_configuration_params['tier']))
        else:
            user_data_filename = 'config/userdata.%s.%s.json' % (stack_type, launch_configuration_params['tier'])

        try:
            with open(user_data_filename, 'r') as f:
                user_data = json.load(f)
                user_data.update({'stack': stack_info})
                user_data.update({'tier': launch_configuration_params['tier']})
                user_data.update({'aws_region': region})
                launch_configuration_params['user_data'] = '''#!/bin/bash
cat > /etc/chef/node.json <<End-of-message
%s
End-of-message
# cd /root/identity-ops && git pull
chef-solo -c /etc/chef/solo.rb -j /etc/chef/node.json''' % json.dumps(user_data, sort_keys=True, indent=4, separators=(',', ': '))
        except IOError:
            # There is no userdata file
            pass
        
        launch_configuration_params['name'] = '%s-%s-%s-%s' % (environment, stack_type, launch_configuration_params['tier'], name)
        # TODO : pull the "key_name" out of the json config
        # and set this per stack_type. prod keys for prod servers etc.

        # for testing just spin everything as t1.micro
        if 'instance_type' not in launch_configuration_params:
            launch_configuration_params['instance_type'] = 't1.micro'
        
        # I'm temporarily giving everything outbound intenret access with the "temp-outbound"
        # security group. TODO : I'll bring our resources (yum, github chef, etc) internal later and close
        # this access
        launch_configuration_params['security_groups'].append('temp-internet')

        # removing because we're blocked by aws on a max of 5 security groups for now
        #launch_configuration_params['security_groups'].append('monitorable')
        
        #launch_configuration_params['security_groups'] = [vpc['security-groups'][environment + '-' + x].id for x in launch_configuration_params['security_groups']]
        launch_configuration_params['security_groups'] = [x.id for x in existing_security_groups if x.name in [environment + '-' + y for y in launch_configuration_params['security_groups']]]

        #ami mapping
        launch_configuration_params['image_id'] = ami_map[launch_configuration_params['image_id']][region]

        #key_name
        launch_configuration_params['key_name'] = key_name

        #enable detailed monitoring
        launch_configuration_params['instance_monitoring'] = True

        #IAM role
        #launch_configuration_params['instance_profile_name'] = '%s-%s' % (environment, launch_configuration_params['tier'])
        if 'instance_profile_name' not in launch_configuration_params:
            launch_configuration_params['instance_profile_name'] = 'identity'

        ag_subnets = [x.id for x in existing_subnets if 'Name' in x.tags and environment + '-' + autoscale_params['subnet'] in x.tags['Name']]
        vpc_zone_identifier = ','.join(ag_subnets)

        if 'scale_method' in autoscale_params and autoscale_params['scale_method'] == 'manual':
            launch_configuration_params['security_group_ids'] = launch_configuration_params['security_groups']
            del(launch_configuration_params['security_groups'])
            launch_configuration_params['monitoring_enabled'] = launch_configuration_params['instance_monitoring']
            del(launch_configuration_params['instance_monitoring'])
            instance_name = launch_configuration_params['name']
            del(launch_configuration_params['name'])

            if 'ebs_optimized' in autoscale_params and autoscale_params['ebs_optimized']:
                launch_configuration_params['ebs_optimized'] = true
            # kernel_id? do we need to set this or is None ok?
            # monitoring_enabled
            
            current_capacity = 0
            for subnet in itertools.cycle([x for x in existing_subnets if x.id in ag_subnets]):
                launch_configuration_params['placement'] = subnet.availability_zone
                launch_configuration_params['subnet_id'] = subnet.id
                reservation = conn_ec2.run_instances(launch_configuration_params)
                current_capacity += 1
                reservation.instances[0].add_tag('Name', instance_name)
                reservation.instances[0].add_tag('App', 'identity')
                reservation.instances[0].add_tag('Env', stack_type)
                if current_capacity >= autoscale_params['desired_capacity'] if 'desired_capacity' in autoscale_params else 1:
                    break
        else:
            del(launch_configuration_params['tier'])
            if 'ebs_optimized' in launch_configuration_params:
                del(launch_configuration_params['ebs_optimized']) # boto doesn't yet support ebsoptimized for autoscaled gropup
            stack['launch_configuration'].append(boto.ec2.autoscale.LaunchConfiguration(**launch_configuration_params))
            launch_configuration = stack['launch_configuration'][-1]
    
            # Don't know what this returns, maybe I should use the return object from create_launch_configuration
            # instead of the instance from the LaunchConfiguration constructor
            # http://docs.aws.amazon.com/AutoScaling/latest/APIReference/API_CreateLaunchConfiguration.html
            # https://github.com/boto/boto/blob/7d1c814c4fecaa69b887e5f1b723ab1f8361cde0/boto/ec2/autoscale/__init__.py#L240
            conn_autoscale.create_launch_configuration(launch_configuration)

            autoscale_group = boto.ec2.autoscale.AutoScalingGroup(
                    group_name=launch_configuration_params['name'], 
                    load_balancers=['%s-%s' % (x, name) for x in autoscale_params['load_balancers']],
                    availability_zones=[region + x for x in availability_zones],
                    launch_config=launch_configuration, 
                    min_size=1, 
                    max_size=12,
                    vpc_zone_identifier=vpc_zone_identifier,
                    desired_capacity=0,
                    connection=conn_autoscale)
            conn_autoscale.create_auto_scaling_group(autoscale_group)
            
            stack['autoscale_group'].append(conn_autoscale.get_all_groups(names=[launch_configuration_params['name']])[0])
            autoscale_group = stack['autoscale_group'][-1]
    
            conn_autoscale.create_or_update_tags([boto.ec2.autoscale.Tag(key='Name',
                                                                         value=launch_configuration_params['name'],
                                                                         propagate_at_launch=True,
                                                                         resource_id=launch_configuration_params['name']),
                                                  boto.ec2.autoscale.Tag(key='App',
                                                                         value='identity',
                                                                         propagate_at_launch=True,
                                                                         resource_id=launch_configuration_params['name']),
                                                  boto.ec2.autoscale.Tag(key='Env',
                                                                         value=stack_type,
                                                                         propagate_at_launch=True,
                                                                         resource_id=launch_configuration_params['name'])])
    
            # Now we set_desired_capacity up from 0 so instances start spinning up
            if mini_stack:
                autoscale_params['desired_capacity'] = 1

            conn_autoscale.set_desired_capacity(launch_configuration_params['name'],
                                                autoscale_params['desired_capacity'] if 'desired_capacity' in autoscale_params else 1)

            # Let's see how it's going
            # conn_autoscale = boto.ec2.autoscale.connect_to_region(region)
            # conn_autoscale.get_all_groups(['identity-dev1-stage-admin-g1'])[0].get_activities()
            # conn_autoscale.get_all_groups(['identity-dev-stage-admin-g1'])[0].get_activities()
        
            # Associate Elastic IP with admin box?

    #stack_filename = "/home/gene/Documents/identity-stack-%s.pkl" % name
    #pickle.dump(stack, open(stack_filename, 'wb'))
    #logging.info('pickled stack to %s' % stack_filename)
    logging.debug('stack %s created' % name)
    return stack

def destroy_stack(region, environment, stack_type, name):
    # Find associated ELBs
    # Find ELB associated Autoscale groups
    # find EIPs associated with proxy instances and delete them
    # destroy AG instances
    # destroy AG and Launchconfig
    # destroy ELBs

    # TODO : check DNS to see that nothing CNAMEs to an ELB with the stack name in it indicating it's in use

    import boto.ec2
    import boto.ec2.elb
    import boto.ec2.autoscale
    import boto.ec2.cloudwatch
    conn_autoscale = boto.ec2.autoscale.connect_to_region(region)
    conn_elb = boto.ec2.elb.connect_to_region(region)
    conn_ec2 = boto.ec2.connect_to_region(region)
    conn_cw = boto.ec2.cloudwatch.connect_to_region(region)

    existing_autoscale_groups = conn_autoscale.get_all_groups()
    existing_launch_configurations = conn_autoscale.get_all_launch_configurations()
    existing_load_balancers = conn_elb.get_all_load_balancers()
    existing_addresses = conn_ec2.get_all_addresses()

    autoscale_groups = []
    launch_configurations = []
    load_balancers = []
    alarms = []
    for autoscale_params in json.load(open('config/autoscale.%s.json' % stack_type, 'r')):
        ag_name = '%s-%s-%s-%s' % (environment, stack_type, autoscale_params['launch_configuration']['tier'], name)
        ag = [x for x in existing_autoscale_groups if x.name == ag_name]
        autoscale_groups.extend(ag)
        launch_configurations.extend([x for x in existing_launch_configurations if x.name == ag_name])

    metric = "HTTPCode_Backend_5XX"

    for load_balancers_params in json.load(open('config/elbs_public.%s.json' % stack_type, 'r')) + json.load(open('config/elbs_private.json')):
        load_balancers_params['name'] = '%s-%s' % (load_balancers_params['name'], name)
        load_balancers.extend([x for x in existing_load_balancers if x.name == load_balancers_params['name']])
        alarms.extend(["%s %s" % (load_balancers_params['name'], metric)])

    # Delete alarms
    conn_cw.delete_alarms(alarms)
    
    # Disassociate EIPs and release them
    for autoscale_group in autoscale_groups:
        for address in [x for x in existing_addresses if x.instance_id in [y.instance_id for y in autoscale_group.instances]]:
            if not conn_ec2.disassociate_address(association_id=address.association_id):
                logging.error('failed to disassociate eip %s from instance %s' % (address.public_ip, address.instance_id))
            if not conn_ec2.release_address(allocation_id=address.allocation_id):
                logging.error('failed to release eip %s' % address.public_ip)

    # Shutdown all instances in the stack
    for autoscale_group in autoscale_groups:
        autoscale_group.shutdown_instances()

    # Wait for all instances to terminate and deleting autoscale groups
    existing_autoscale_groups = conn_autoscale.get_all_groups()
    for autoscale_group in autoscale_groups:
        attempts=0
        while True:
            attempts += 1
            remaining_live_instances = len([x.instances for x in existing_autoscale_groups if x.name == autoscale_group.name][0])
            if remaining_live_instances == 0:
                autoscale_group.delete()
                break
            else:
                logging.debug('waiting 10 seconds for remaining %s instances in the %s autoscale group to finish shutting down' % (remaining_live_instances, autoscale_group.name))
                time.sleep(10)
                existing_autoscale_groups = conn_autoscale.get_all_groups([x.name for x in autoscale_groups])
                if attempts > 30:
                    logging.error('unable to delete autoscale group %s after 5 minutes' % autoscale_group.name)
                    autoscale_group.get_activities()
                    autoscale_group.delete()
                    break

    # Delete launch configurations
    for launch_configuration in launch_configurations:
        launch_configuration.delete()

    # Delete load balancers
    for load_balancer in load_balancers:
        load_balancer.delete()
    logging.debug('stack %s destroyed' % name)

def get_stack(region, environment, stack_type, name):
    import pprint
    import boto.ec2
    import boto.ec2.elb
    import json
    #import boto.ec2.autoscale
    #conn_autoscale = boto.ec2.autoscale.connect_to_region(region)
    conn_elb = boto.ec2.elb.connect_to_region(region)
    conn_ec2 = boto.ec2.connect_to_region(region)
    #existing_autoscale_groups = conn_autoscale.get_all_groups()
    #existing_launch_configurations = conn_autoscale.get_all_launch_configurations()
    existing_load_balancers = conn_elb.get_all_load_balancers()
    #existing_addresses = conn_ec2.get_all_addresses()

    output = {}
    output['instances'] = {}
    output['load balancers'] = {}
    reservations = conn_ec2.get_all_instances(None, {"tag:Name" : "*-%s" % name,
                                                     "tag:Env"  : stack_type})
    reservations.extend(conn_ec2.get_all_instances(None, {"tag:Name" : "*-univ",
                                                          "tag:Env"  : stack_type}))
    for reservation in reservations:
        for instance in reservation.instances:
            if instance.state == 'running':
                output['instances'][instance.id] = {'Name'               : instance.tags['Name'],
                                                    'private_ip_address' : instance.private_ip_address}
                if instance.ip_address:
                    output['bastion_ip'] = instance.ip_address
    output['instance_ip_list'] = " ".join([output['instances'][x]['private_ip_address'] for x in output['instances'].keys()])
    for load_balancer in [x for x in existing_load_balancers if x.name[-len(name) - 1:] == "-%s" % name]:
        lb_instances = [{'id': x.id, 
                         'Name' : output['instances'][x.id]['Name'], 
                         'private_ip_address' : output['instances'][x.id]['private_ip_address']} for x in load_balancer.instances]
        output['load balancers'][load_balancer.name] = {'dns_name' : load_balancer.dns_name,
                                                        'instances': lb_instances}
    return output

def show_stack(region, environment, stack_type, name):
    output=get_stack(region, environment, stack_type, name)
    for x in output.keys():
        print x
        print json.dumps(output[x], sort_keys=True, indent=4, separators=(',', ': '))

def point_dns_to_stack(environment, stack_type, name):
    import sys
    from dynect.DynectDNS import DynectRest # sudo pip install https://github.com/dyninc/Dynect-API-Python-Library/zipball/master
    
    rest_iface = DynectRest()

    if 'AWS_CONFIG_DIR' in os.environ:
        user_data_filename = os.path.join(os.environ['AWS_CONFIG_DIR'], 'dynect.json')
    else:
        user_data_filename = 'config/dynect.json'

    with open(user_data_filename, 'r') as f:
        dynect_credentials = json.load(f)
    
    # Log in
    response = rest_iface.execute('/Session/', 'POST', dynect_credentials)
    
    if response['status'] != 'success':
      sys.exit("Incorrect credentials")
    
    # Perform action
    response = rest_iface.execute('/CNAMERecord/anosrep.org/www.anosrep.org/', 'GET')
    
    # Log out, to be polite
    rest_iface.execute('/Session/', 'DELETE')

if __name__ == '__main__':
    path = "/identity/"

    #region = 'us-west-1'
    #availability_zones = ['b','c']

    region = 'us-west-2'
    availability_zones = ['a','b','c']

    #region = 'us-east-1'
    #availability_zones = ['a','b','d']
   
    environment = 'identity-dev'

#     stack = create_stack(region,
#                          environment, 
#                          'stage', 
#                          availability_zones, 
#                          path,
#                          False, 
#                          '0501',
#                          None,
#                          False
#                          )
    destroy_stack(region,
                  environment,
                  'stage',
                  '0502')
#     show_stack(region,
#                environment,
#                'stage',
#                '0501')
