'''Articulated "skeleton" class and associated helper functions.'''

import climate
import numpy as np
import ode

from . import parser

logging = climate.get_logger(__name__)


def pid(kp=0., ki=0., kd=0., smooth=0.1):
    '''Create a callable that implements a PID controller.

    A PID controller returns a control signal u(t) given a history of error
    measurements e(0) ... e(t), using proportional (P), integral (I), and
    derivative (D) terms, according to:

    u(t) = kp * e(t) + ki * \int_{s=0}^t e(s) ds + kd * \frac{de(s)}{ds}(t)

    The proportional term is just the current error, the integral term is the
    sum of all error measurements, and the derivative term is the instantaneous
    derivative of the error measurement.

    Parameters
    ----------
    kp : float
        The weight associated with the proportional term of the PID controller.
    ki : float
        The weight associated with the integral term of the PID controller.
    kd : float
        The weight associated with the derivative term of the PID controller.
    smooth : float in [0, 1]
        Derivative values will be smoothed with this exponential average. A
        value of 1 never incorporates new derivative information, a value of 0.5
        uses the mean of the historic and new information, and a value of 0
        discards historic information (i.e., the derivative in this case will be
        unsmoothed). The default is 0.1.

    Returns
    -------
    (error, dt) -> control
        Returns a function that accepts an error measurement and a delta-time
        value since the previous measurement, and returns a control signal.
    '''
    state = dict(p=0, i=0, d=0)
    def control(error, dt=1):
        state['d'] = smooth * state['d'] + (1 - smooth) * (error - state['p']) / dt
        state['i'] += error * dt
        state['p'] = error
        return kp * state['p'] + ki * state['i'] + kd * state['d']
    return control


def flat_array(iterables):
    '''Given a sequence of sequences, return a flat numpy array.

    Parameters
    ----------
    iterables : sequence of sequence of number
        A sequence of tuples or lists containing numbers. Typically these come
        from something that represents each joint in a skeleton, like angle.

    Returns
    -------
    ndarray :
        An array of flattened data from each of the source iterables.
    '''
    arr = []
    for x in iterables:
        arr.extend(x)
    return np.array(arr)


class Skeleton:
    '''A skeleton is a group of rigid bodies connected with articulated joints.

    Commonly, the skeleton is used to represent an articulated body that is
    capable of mimicking the motion of the human body.

    Most often, skeletons are configured by parsing information from a text file
    of some sort. See :class:`pagoda.parser.Parser` for more information about
    the format of the text file.

    Parameters
    ----------
    world : class:`pagoda.physics.World`
        A world object that holds bodies and joints for physics simulation.

    Attributes
    ----------
    bodies : list of class:`pagoda.physics.Body`
        A list of the rigid bodies that comprise this skeleton.

    joints : list of class:`pagoda.physics.Joint`
        A list of the joints that connect bodies in this skeleton.

    roots : list of class:`pagoda.physics.Body`
        A list of rigid bodies that are considered "roots" in the skeleton.
        Roots are given special treatment when modeling movement of the
        skeleton; for instance, when using ASM data, the skeleton root is
        allowed to interact with the world, and when using the Cooper model,
        roots are allowed to remain attached to their associated markers during
        the inverse dynamics process.
    '''

    def __init__(self, world):
        self.world = world
        self.jointgroup = ode.JointGroup()

        self.roots = []
        self.bodies = []
        self.joints = []

    def load_skel(self, source):
        '''Load a skeleton definition from a text file.

        Parameters
        ----------
        source : str or file
            A filename or file-like object that contains text information
            describing a skeleton. See :class:`pagoda.parser.Parser` for more
            information about the format of the text file.
        '''
        logging.info('%s: parsing skeleton configuration', source)
        p = parser.Parser(self.world, self.jointgroup)
        p.parse(source)
        self.roots = [world.get_body(r) for r in p.roots]
        self.bodies = p.bodies
        self.joints = p.joints
        self.set_pid_params(kp=(1 - 1e4) / self.world.dt)

    def load_asf(self, source):
        '''Load a skeleton definition from an ASF text file.

        NOT IMPLEMENTED!

        Parameters
        ----------
        source : str or file
            A filename or file-like object that contains text information
            describing a skeleton, in ASF format.
        '''
        raise NotImplementedError

    def set_pid_params(self, *args, **kwargs):
        '''Set PID parameters for all joints in the skeleton.

        Parameters for this method are passed directly to the `pid` constructor.
        '''
        for joint in self.joints:
            joint.target_angles = [None] * joint.ADOF
            joint.controllers = [pid(*args, **kwargs) for i in range(joint.ADOF)]

    @property
    def num_dofs(self):
        '''Return the number of degrees of freedom in the skeleton.'''
        return sum(j.ADOF for j in self.joints)

    @property
    def joint_angles(self):
        '''Get a list of all current joint angles in the skeleton.'''
        return flat_array(j.angles for j in self.joints)

    @property
    def joint_velocities(self):
        '''Get a list of all current joint velocities in the skeleton.'''
        return flat_array(j.velocities for j in self.joints)

    @property
    def joint_torques(self):
        '''Get a list of all current joint torques in the skeleton.'''
        return flat_array(j.amotor.feedback[-1][:j.ADOF] for j in self.joints)

    @property
    def body_positions(self):
        '''Get a list of all current body positions in the skeleton.'''
        return flat_array(b.position for b in self.bodies)

    @property
    def body_rotations(self):
        '''Get a list of all current body rotations in the skeleton.'''
        return flat_array(b.quaternion for b in self.bodies)

    @property
    def body_linear_velocities(self):
        '''Get a list of all current body velocities in the skeleton.'''
        return flat_array(b.linear_velocity for b in self.bodies)

    @property
    def body_angular_velocities(self):
        '''Get a list of all current body angular velocities in the skeleton.'''
        return flat_array(b.angular_velocity for b in self.bodies)

    def indices_for_joint(self, name):
        '''Get a list of the indices for a specific joint.

        Parameters
        ----------
        name : str
            The name of the joint to look up.

        Returns
        -------
        list of int :
            A list of the index values for quantities related to the named
            joint. Often useful for getting, say, the angles for a specific
            joint in the skeleton.
        '''
        j = 0
        for joint in self.joints:
            if joint.name == name:
                return list(range(j, j + joint.ADOF))
            j += joint.ADOF
        return []

    def indices_for_body(self, name, step=3):
        '''Get a list of the indices for a specific body.

        Parameters
        ----------
        name : str
            The name of the body to look up.
        step : int, optional
            The number of numbers for each body. Defaults to 3, should be set
            to 4 for body rotation (since quaternions have 4 values).

        Returns
        -------
        list of int :
            A list of the index values for quantities related to the named body.
        '''
        for j, body in enumerate(self.bodies):
            if body.name == name:
                return list(range(j * step, (j + 1) * step))
        return []

    def joint_rms(self):
        '''Get the current RMS joint separations for the skeleton.

        Returns
        -------
        float :
            A scalar value expressing the RMS distance, over all joints in the
            skeleton, between the two anchor points in the joint. This quantity
            describes how "exploded" the bodies in the skeleton are. A value of
            0 indicates that all joint constraints are perfectly satisfied.
        '''
        deltas = []
        for joint in self.joints:
            delta = np.array(joint.anchor) - joint.anchor2
            deltas.append((delta * delta).sum())
        return np.sqrt(np.mean(deltas))

    def get_body_states(self):
        '''Return a list of the states of all bodies in the skeleton.'''
        return [(b.name,
                 b.position,
                 b.quaternion,
                 b.linear_velocity,
                 b.angular_velocity) for b in self.bodies]

    def set_body_states(self, states):
        '''Set the states of all bodies in the skeleton.'''
        for name, pos, quat, lin, ang in states:
            body = self.world.get_body(name)
            body.position = pos
            body.quaternion = quat
            body.linear_velocity = lin
            body.angular_velocity = ang

    def set_joint_velocities(self, target=0):
        '''Set the target velocity for all joints in the skeleton.

        Often the target is set to 0 to cancel out any desired joint rotation.

        Parameters
        ----------
        target : float
            The target velocity for all joints in the skeleton.
        '''
        for joint in self.joints:
            joint.velocities = target

    def enable_motors(self, max_force):
        '''Enable the joint motors in this skeleton.

        This method sets the maximum force that can be applied by each joint to
        attain the desired target velocities. It also enables torque feedback
        for all joint motors.

        Parameters
        ----------
        max_force : float
            The maximum force that each joint is allowed to apply to attain its
            target velocity.
        '''
        for joint in self.joints:
            joint.max_forces = max_force
            joint.amotor.ode_motor.setFeedback(True)

    def disable_motors(self):
        '''Disable joint motors in this skeleton.

        This method sets to 0 the maximum force that joint motors are allowed to
        apply, in addition to disabling torque feedback.
        '''
        for joint in self.joints:
            joint.max_forces = 0
            joint.amotor.ode_motor.setFeedback(False)

    def set_target_angles(self, angles):
        '''Move each joint toward a target angle.

        This method uses a PID controller to set a target angular velocity for
        each degree of freedom in the skeleton, based on the difference between
        the current and the target angle for the respective DOF.

        PID parameters are by default set to achieve a tiny bit less than
        complete convergence in one time step, using only the P term (i.e., the
        P coefficient is set to 1 - \delta, while I and D coefficients are set
        to 0). PID parameters can be updated by calling the `set_pid_params`
        method.

        Parameters
        ----------
        angles : list of float
            A list of the target angles for every joint in the skeleton.
        '''
        j = 0
        for joint in self.joints:
            velocities = [
                ctrl(tgt - cur, self.world.dt) for cur, tgt, ctrl in
                zip(joint.angles, angles[j:j+joint.ADOF], joint.controllers)]
            joint.velocities = velocities
            j += joint.ADOF

    def add_torques(self, torques):
        '''Add torques for each degree of freedom in the skeleton.

        Parameters
        ----------
        torques : list of float
            A list of the torques to add to each degree of freedom in the
            skeleton.
        '''
        j = 0
        for joint in self.joints:
            joint.add_torques(
                list(torques[j:j+joint.ADOF]) + [0] * (3 - joint.ADOF))
            j += joint.ADOF
