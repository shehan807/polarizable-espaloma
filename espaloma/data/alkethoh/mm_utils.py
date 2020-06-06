import numpy as onp
from jax import numpy as np
from jax import jit, grad


## Compute energies

# Bonds
@jit
def compute_distances(xyz, pair_inds):
    """
    xyz.shape : (n_snapshots, n_atoms, n_dim)
    pair_inds.shape : (n_pairs, 2)
    """

    diffs = (xyz[:, pair_inds[:, 0]] - xyz[:, pair_inds[:, 1]])
    return np.sqrt(np.sum(diffs ** 2, axis=2))


@jit
def harmonic_bond_potential(r, k, r0):
    return 0.5 * k * (r0 - r) ** 2



# Angles
@jit
def angle(a, b, c):
    """a,b,c each have shape (n_snapshots, n_angles, dim)"""

    u = b - a
    u /= np.sqrt(np.sum(u ** 2, axis=2))[:, :, np.newaxis]

    v = c - b
    v /= np.sqrt(np.sum(v ** 2, axis=2))[:, :, np.newaxis]

    udotv = np.sum(u * v, axis=2)

    return np.arccos(-udotv)


@jit
def compute_angles(xyz, inds):
    a, b, c = xyz[:, inds[:, 0]], xyz[:, inds[:, 1]], xyz[:, inds[:, 2]]
    return angle(a, b, c)


def harmonic_angle_potential(theta, k, theta0):
    return 0.5 * k * (theta0 - theta) ** 2


# Torsions
@jit
def dihedral_angle(a, b, c, d):
    b1 = b - a
    # b2 = c - b # mdtraj convention
    b2 = b - c  # openmm convention
    b3 = d - c

    c1 = np.cross(b2, b3)
    c2 = np.cross(b1, b2)

    p1 = np.sum(b1 * c1, axis=2) * np.sum(b2 * b2, axis=2) ** 0.5
    p2 = np.sum(c1 * c2, axis=2)

    return np.arctan2(p1, p2)


@jit
def compute_torsions(xyz, inds):
    a, b, c, d = xyz[:, inds[:, 0]], xyz[:, inds[:, 1]], xyz[:, inds[:, 2]], xyz[:, inds[:, 3]]
    return dihedral_angle(a, b, c, d)


@jit
def periodic_torsion_potential(theta, ks, phases, periodicities):
    return np.sum([ks[:, i] * (1 + np.cos(periodicities[:, i] * theta - phases[:, i])) for i in range(n_periodicities)],
                  axis=0)


# Nonbonded
from jax import vmap


def pdist(x):
    """should be consistent with scipy.spatial.pdist:
    flat, non-redundant pairwise distances"""
    diffs = np.expand_dims(x, 1) - np.expand_dims(x, 0)
    squared_distances = np.sum(diffs ** 2, axis=2)
    inds = onp.triu_indices_from(squared_distances, k=1)
    return np.sqrt(squared_distances[inds])


# openmm
from simtk import unit
from simtk import openmm as mm
from simtk.openmm.app import Simulation
from simtk.openmm import XmlSerializer


def get_sim(name):
    """nonbonded forces in group 0, all other forces in group 1"""
    mol = offmols[name]

    # Parametrize the topology and create an OpenMM System.
    topology = mol.to_topology()

    with open(data_path + 'snapshots_and_energies/{}_system.xml'.format(name), 'r') as f:
        xml = f.read()

    system = XmlSerializer.deserializeSystem(xml)

    platform = mm.Platform.getPlatformByName('Reference')
    integrator = mm.VerletIntegrator(1.0)

    sim = Simulation(topology, system, integrator, platform=platform)

    inds_of_nb_forces = [i for i in range(sim.system.getNumForces()) if
                         'Nonbonded' in sim.system.getForce(i).__class__.__name__]

    for i in range(sim.system.getNumForces()):
        sim.system.getForce(i).setForceGroup(1)
    for i in inds_of_nb_forces:
        sim.system.getForce(i).setForceGroup(0)

    return sim


def set_positions(sim, pos):
    sim.context.setPositions(pos)


def get_energy(sim):
    return sim.context.getState(getEnergy=True).getPotentialEnergy() / unit.kilojoule_per_mole


def get_nb_energy(sim):
    """assumes NonbondedForce is in group 0"""
    return sim.context.getState(getEnergy=True, groups={0}).getPotentialEnergy() / unit.kilojoule_per_mole
