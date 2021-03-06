#!/usr/bin/env python

"""
Energy minimization of system in a water box.
Uses a rhombic dodecahedron box and the Amber99sb-ILDN FF (w/ TIP3P).
Default padding of 1 nm from protein to edge of the box.
Neutralizes the system and adds ions to 0.15 M.

.2017. joaor@stanford.edu
"""

from __future__ import print_function, division

import argparse
import logging
import os
import random
import sys

import numpy as np

# cython-optmized pairwise distance function
# profiled to run in ~50% of the time of pdist
# much less memory hungry. No storage of all distances.
try:
    # add to PYTHONPATH current workdir and script dir
    sys.path.insert(0, os.curdir)
    sys.path.insert(0, os.path.dirname(__file__))

    from _pwdistance import pw_dist
    opt_pw = True
except ImportError, e:
    opt_pw = False
    from scipy.spatial.distance import pdist

    def pw_dist(xyz_array):
        return np.amax(pdist(xyz_array, 'euclidean'))

import simtk.openmm.app as app
import simtk.openmm as mm
import simtk.unit as units

##
# Parse user input and options
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('pdb', help='Input PDB file')
ap.add_argument('--log', type=str, help='Log file name')
ap.add_argument('--pad', type=float, default=1.0, help='Box Padding in nm')
ap.add_argument('--seed', type=int, default=917, help='Random Number Seed')
ap.add_argument('--vacuum', action="store_true", help='Does EM in vacuum')
ap.add_argument('--noadd', action="store_true", help='Skips missing atoms check')

opt_plat = ap.add_mutually_exclusive_group()
opt_plat.add_argument('--cpu', action="store_true", help='Use CPU platform')
opt_plat.add_argument('--cuda', action="store_true", help='Use CUDA platform')

ap.set_defaults(cpu=False, cuda=True)
user_args = ap.parse_args()

# Set random seed for all Python processed (integrators too, further below)
random.seed(user_args.seed)

# Format logger
if not user_args.log:
    logfile = sys.stdout
else:
    logfile = open(user_args.log, 'w')

logging.basicConfig(stream=logfile,
                    level=logging.INFO,
                    format='[%(asctime)s] %(message)s',
                    datefmt='%Y/%m/%d %H:%M:%S')

logging.info('Starting Simulation')

if not os.path.isfile(user_args.pdb):
    raise IOError('Could not read/open input file: {}'.format(user_args.pdb))
else:
    rootname = os.path.basename(user_args.pdb)[:-4]

# Define platform: CPU/CUDA
gpu_res = os.getenv('CUDA_VISIBLE_DEVICES')
cpu_res = os.getenv('SLURM_CPUS_PER_TASK')
properties = {}
if user_args.cuda:
    if not gpu_res:
        logging.error('No CUDA GPUs detected')
        sys.exit(1)

    platform = mm.Platform.getPlatformByName('CUDA')

    num_gpu = len(gpu_res.split(','))
    if num_gpu == 1:
        properties = {}
    else:
        logging.info('Using {}/{} CPUs/GPUs'.format(cpu_res, num_gpu))
        properties['DeviceIndex'] = gpu_res
else:
    platform = mm.Platform.getPlatformByName('CPU')
    properties['Threads'] = cpu_res

logging.info('Using platform: {}'.format(platform.getName()))

# Read PDB file
logging.info('Reading PDB file: {}'.format(user_args.pdb))
pdb = app.PDBFile(user_args.pdb)
forcefield = app.ForceField('amber99sbildn.xml', 'tip3p.xml')

# Processing structure and build box
modeller = app.Modeller(pdb.topology, pdb.positions)
if not user_args.noadd:
    logging.info('Adding missing atoms')
    modeller.addHydrogens(forcefield, pH=7.0, platform=platform)  # already does EM

# Build rhombic dodecahedron box (square xy-plane)
# 0. Center system at origin
logging.info('Moving system to origin')
com_xyz = modeller.positions.mean()
for i, xyz_i in enumerate(modeller.positions):
    modeller.positions[i] = xyz_i - com_xyz

# 1. Move coordinates to numpy array for efficiency
logging.info('Calculating optimal box size')
_xyz = [(x._value, y._value, z._value) for x, y, z in modeller.positions]
xyz = np.array(_xyz, dtype=np.float)
xyz_size = np.amax(xyz, axis=0) - np.amin(xyz, axis=0)
if not opt_pw:
    logging.info('Using slower pairwise distance calculation routine')
xyz_diam = pw_dist(xyz)

d = xyz_diam + user_args.pad*2
u = np.array((d, 0, 0))
v = np.array((0, d, 0))
w = np.array((d/2, d/2, np.sqrt(2)*d/2))
box_vol = 0.5 * np.sqrt(2) * np.power(d, 3)

n_atm = modeller.topology.getNumAtoms()
n_res = modeller.topology.getNumResidues()

logging.info('System      : {:6d} Atoms {:6d} Residues'.format(n_atm, n_res))
logging.info('System Size : {:6.3f} {:6.3f} {:6.3f}'.format(*xyz_size))
logging.info('Diameter    : {:6.3f}'.format(xyz_diam))
logging.info('Box Volume  : {:6.3f}'.format(box_vol))
logging.info('Box Vectors :')
logging.info('  u = {:6.3f} {:6.3f} {:6.3f}'.format(*u))
logging.info('  v = {:6.3f} {:6.3f} {:6.3f}'.format(*v))
logging.info('  w = {:6.3f} {:6.3f} {:6.3f}'.format(*w))

modeller.topology.setPeriodicBoxVectors((u, v, w))

if not user_args.vacuum:
    # Solvate the Box and add counter ions at 0.15 M
    logging.info('Solvating the system')
    modeller.addSolvent(forcefield, model='tip3p',
                        neutralize=True, ionicStrength=0.15*units.molar)

    resname_list = [r.name for r in modeller.topology.residues()]
    n_waters = resname_list.count('HOH')
    n_cation = resname_list.count('NA')
    n_anion = resname_list.count('CL')

    logging.info('  num. waters: {:6d}'.format(n_waters))
    logging.info('  num. ions: {:6d} Na {:6d} Cl'.format(n_cation, n_anion))

# Create System
system = forcefield.createSystem(modeller.topology,
                                 nonbondedMethod=app.PME,
                                 nonbondedCutoff=1*units.nanometer,
                                 constraints=app.HBonds)

# Add restraints on protein heavy atoms
# Breaking for some reason..
# logging.info('Adding pos. res. on non-hydrogen protein atoms')
all_atoms = list(modeller.topology.atoms())
posre = mm.CustomExternalForce("0.5*k*((x-x0)^2+(y-y0)^2+(z-z0)^2)")
posre_K = 100.0
posre.addGlobalParameter("k", posre_K * (units.kilojoule_per_mole/units.nanometer**2))  # noqa: E501
posre.addPerParticleParameter("x0")
posre.addPerParticleParameter("y0")
posre.addPerParticleParameter("z0")

solvent = set(('HOH', 'NA', 'CL'))
n_at = 0
for i, atom_crd in enumerate(modeller.getPositions()):
    at = all_atoms[i]
    if at.residue.name not in solvent and at.element != app.element.hydrogen:
        n_at += 1
        posre.addParticle(i, atom_crd.value_in_unit(units.nanometers))
system.addForce(posre)
logging.info('{}/{} atoms restrained (Fc={:.3f} kJ/mol.nm^2)'.format(n_at, len(all_atoms), posre_K))  # noqa: E501

# Setup System
md_temp = 1*units.kelvin
md_step = 0.002*units.picoseconds
md_fric = 1/units.picosecond
integrator = mm.LangevinIntegrator(md_temp, md_fric, md_step)
integrator.setRandomNumberSeed(user_args.seed)

simulation = app.Simulation(modeller.topology, system, integrator,
                            platform=platform, platformProperties=properties)
simulation.context.setPositions(modeller.positions)

##
# Minimize
state = simulation.context.getState(getEnergy=True, getPositions=True)
pot_ene = state.getPotentialEnergy().value_in_unit_system(units.md_unit_system)
logging.info('Initial Potential Energy: {:10.3f}'.format(pot_ene))

# Write initial coordinates
positions = state.getPositions()
fname = "{}_initial.pdb".format(rootname)
with open(fname, 'w') as handle:
    app.PDBFile.writeFile(simulation.topology, positions, handle)

logging.info('Running energy minimization')
simulation.minimizeEnergy(maxIterations=1000,
                          tolerance=10*units.kilojoule/units.mole)

state = simulation.context.getState(getEnergy=True, getPositions=True)
pot_ene = state.getPotentialEnergy().value_in_unit_system(units.md_unit_system)
logging.info('Potential Energy after minimization: {:10.3f}'.format(pot_ene))

# Write minimized file
positions = state.getPositions()
fname = "{}_minimized.pdb".format(rootname)
with open(fname, 'w') as handle:
    app.PDBFile.writeFile(simulation.topology, positions, handle)

logging.info('Done')
logging.shutdown()
