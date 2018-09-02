import argparse
import rospy
from pidrone_pkg.msg import State
import subprocess
import os


class StateEstimator(object):
    """
    This class is intended to unify the different state estimators so that the
    user only has to call this script to interact with state estimators. This
    node publishes to /pidrone/state, which offers the best state estimate
    based on whichever state estimators are to be used, depending on the
    command-line arguments that the user passes into this script.
    
    The different state estimators are:
        - EMA: uses an exponential moving average
        - UKF with a 2D state vector
        - UKF with a 7D state vector
        - Simulation (drone_simulator.py): provides simulated ground truth
        
    This script runs the state estimators in non-blocking subprocesses. When
    this script is terminating, there is a finally clause that will attempt to
    terminate the subprocesses. Note that this needs to be tested well, and that
    the shell=True argument for these subprocesses could be a security hazard
    that should be investigated further.
    """
    
    def __init__(self, primary, others, ir_throttled=False, imu_throttled=False,
                 optical_flow_throttled=False, camera_pose_throttled=False, sdim=1):
        self.state_msg = State()
        
        self.primary_estimator = primary
        self.other_estimators = others
        
        self.process_cmds_dict = {
                'ema': 'python StateEstimators/state_estimator_ema.py',
                'ukf2d': 'python StateEstimators/student_state_estimator_ukf_2d.py',
                'ukf7d': 'python StateEstimators/student_state_estimator_ukf_7d.py',
                'simulator': 'rosrun pidrone_pkg drone_simulator.py --dim '+str(sdim)
        }
        # TODO: Test that the above rosrun and command-line argument passing works
        
        self.can_use_throttled_ir = ['ukf2d', 'ukf7d']
        self.can_use_throttled_imu = ['ukf2d', 'ukf7d']
        self.can_use_throttled_optical_flow = ['ukf7d']
        self.can_use_throttled_camera_pose = ['ukf7d']
        
        # List to store the process objects from subprocess.Popen()
        self.processes = []

        self.ukf_topics = {2: '/pidrone/state/ukf_2d',
                           7: '/pidrone/state/ukf_7d'}
        self.ema_topic = '/pidrone/state/ema'
        # TODO: Get the drone_simulator to publish State messages to this topic
        self.simulator_topic = '/pidrone/state/simulator'

        self.state_pub = rospy.Publisher('/pidrone/state', State, queue_size=1,
                                         tcp_nodelay=False)

        self.start_estimator_subprocess_cmds()
        self.initialize_ros()

    def initialize_ros(self):
        node_name = os.path.splitext(os.path.basename(__file__))[0]
        rospy.init_node(node_name)

        rospy.spin()
    
    def start_estimator_subprocess_cmds(self):
        cmd = self.process_cmds_dict[self.primary_estimator]
        cmd = self.append_throttle_flags(cmd, self.primary_estimator)
        process_cmds = [cmd]
        if self.primary_estimator == 'ukf2d':
            # We want the EMA to provide x and y position and velocity
            # estimates, for example, to supplement the 2D UKF's z position and
            # velocity estimates.
            process_cmds.append(self.process_cmds_dict['ema'])
            self.ema_state_msg = State()
            rospy.Subscriber(self.ema_topic, State, self.ema_helper_callback)
            rospy.Subscriber(self.ukf_topics[2], State, self.state_callback)
        elif self.primary_estimator == 'ukf7d':
            rospy.Subscriber(self.ukf_topics[7], State, self.state_callback)
        elif self.primary_estimator == 'ema':
            rospy.Subscriber(self.ema_topic, State, self.state_callback)
        elif self.primary_estimator == 'simulator':
            rospy.Subscriber(self.simulator_topic, State, self.state_callback)
        
        # Set up the process commands for the non-primary estimators
        if self.other_estimators is not None:
            for other_estimator in self.other_estimators:
                # Avoid running a subprocess more than once
                if other_estimator not in process_cmds:
                    other_cmd = self.process_cmds_dict[other_estimator]
                    other_cmd = self.append_throttle_flags(other_cmd, other_estimator)
                    process_cmds.append(other_cmd)
            
        for p in process_cmds:
            print 'Starting:', p
            # NOTE: shell=True could be security hazard
            self.processes.append((p, subprocess.Popen(p, shell=True)))
            
    def append_throttle_flags(self, cmd, estimator):
        if estimator in self.can_use_throttled_ir:
            cmd += ' --ir_throttled'
        if estimator in self.can_use_throttled_imu:
            cmd += ' --imu_throttled'
        if estimator in self.can_use_throttled_optical_flow:
            cmd += ' --optical_flow_throttled'
        if estimator in self.can_use_throttled_camera_pose:
            cmd += ' --camera_pose_throttled'
        return cmd

    def state_callback(self, msg):
        """
        Callback that handles the primary estimator republishing.
        """
        
        # TODO: Consider creating a new State message rather than modifying just
        #       one State message
        self.state_msg.header.stamp = rospy.Time.now()
        if self.primary_estimator == 'ukf2d':
            # Use EMA data for x and y positions and velocities
            x = self.ema_state_msg.pose_with_covariance.pose.position.x
            y = self.ema_state_msg.pose_with_covariance.pose.position.y
            vel_x = self.ema_state_msg.twist_with_covariance.twist.linear.x
            vel_y = self.ema_state_msg.twist_with_covariance.twist.linear.y
        else:
            # Use primary_estimator data for x and y positions and velocities
            x = msg.pose_with_covariance.pose.position.x
            y = msg.pose_with_covariance.pose.position.y
            vel_x = msg.twist_with_covariance.twist.linear.x
            vel_y = msg.twist_with_covariance.twist.linear.y
        
        z = msg.pose_with_covariance.pose.position.z
        vel_z = msg.twist_with_covariance.twist.linear.z
        orientation = msg.pose_with_covariance.pose.orientation
        vel_angular = msg.twist_with_covariance.twist.angular
        
        self.state_msg.pose_with_covariance.pose.position.x = x
        self.state_msg.pose_with_covariance.pose.position.y = y
        self.state_msg.pose_with_covariance.pose.position.z = z
        self.state_msg.pose_with_covariance.pose.orientation = orientation
        self.state_msg.twist_with_covariance.twist.linear.x = vel_x
        self.state_msg.twist_with_covariance.twist.linear.y = vel_y
        self.state_msg.twist_with_covariance.twist.linear.z = vel_z
        self.state_msg.twist_with_covariance.twist.angular = vel_angular
        
        # Include covariances
        self.state_msg.pose_with_covariance.covariance = msg.pose_with_covariance.covariance
        self.state_msg.twist_with_covariance.covariance = msg.twist_with_covariance.covariance
        
        self.state_pub.publish(self.state_msg)
        
    def ema_helper_callback(self, msg):
        """
        When the primary estimator is the 2D UKF, populate self.ema_state_msg
        in this callback.
        """
        self.ema_state_msg.pose_with_covariance.pose.position.x = msg.pose_with_covariance.pose.position.x
        self.ema_state_msg.pose_with_covariance.pose.position.y = msg.pose_with_covariance.pose.position.y
        self.ema_state_msg.twist_with_covariance.twist.linear.x = msg.twist_with_covariance.twist.linear.x
        self.ema_state_msg.twist_with_covariance.twist.linear.y = msg.twist_with_covariance.twist.linear.y
        

def main():
    parser = argparse.ArgumentParser(description=('The state estimator node '
                'can provide state estimates using a 1D UKF (2D state vector), '
                'a 3D UKF (7D state vector), an EMA, or simulated ground truth '
                'data. The default is the EMA. The primary state estimator '
                'determines what is published to /pidrone/state, except that '
                'an incomplete state estimator like the 2D UKF will also use '
                'EMA estimates to populate x and y position, for example.'))
                
    arg_choices = ['ema', 'ukf2d', 'ukf7d', 'simulator']
    
    parser.add_argument('--primary', '-p',
                        choices=arg_choices,
                        default='ema',
                        help='Select the primary state estimation method')
    parser.add_argument('--others', '-o',
                        choices=arg_choices,
                        nargs='+',
                        help=('Select other state estimation nodes to run '
                              'alongside the primary state estimator, e.g., '
                              'for visualization or debugging purposes'))
                              
    # Arguments to determine if the throttle command is being used. E.g.:
    #   rosrun topic_tools throttle messages /pidrone/infrared 40.0
    # If one of these is passed in, it will act on all state estimators that can
    # take it in as a command-line argument.
    parser.add_argument('--ir_throttled', action='store_true',
                        help=('Use throttled infrared topic /pidrone/infrared_throttle'))
    parser.add_argument('--imu_throttled', action='store_true',
                        help=('Use throttled IMU topic /pidrone/imu_throttle'))
    parser.add_argument('--optical_flow_throttled', action='store_true',
                        help=('Use throttled optical flow topic /pidrone/picamera/twist_throttle'))
    parser.add_argument('--camera_pose_throttled', action='store_true',
                        help=('Use throttled camera pose topic /pidrone/picamera/pose_throttle'))
                        
    parser.add_argument('--sdim', default=1, type=int, choices=[1, 2, 3],
                        help=('Number of spatial dimensions in which to '
                              'simulate the drone\'s motion, if running the '
                              'drone simulator (default: 1)'))
                              
    args = parser.parse_args()
    
    try:
        se = StateEstimator(primary=args.primary,
                            others=args.others,
                            ir_throttled=args.ir_throttled,
                            imu_throttled=args.imu_throttled,
                            optical_flow_throttled=args.optical_flow_throttled,
                            camera_pose_throttled=args.camera_pose_throttled,
                            sdim=args.sdim)
    except Exception as e:
        print e
    finally:
        # Terminate the subprocess calls. Note, however, that if Ctrl-C is
        # entered in stdin, it seems that the subprocesses also get the Ctrl-C
        # input and are terminating based on KeyboardInterrupt
        print 'Terminating subprocess calls...'
        for process_name, process in se.processes:
            print 'Terminating:', process_name
            process.terminate()
        print 'Done.'


if __name__ == "__main__":
    main()
