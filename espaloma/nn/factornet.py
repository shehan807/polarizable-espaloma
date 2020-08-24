"""
pass messages on factor graph
"""


import numpy as np
import torch

import dgl
import dgl.function as fn
import dgllife

import torch.nn as nn
import torch.nn.functional as F

from openforcefield.topology import Molecule

# TODO: use an atom embedding or something here instead
allowable_atomic_numbers = [1,6,7,8]
ATOM_DIM = len(allowable_atomic_numbers)


def one_hot_elements(atomic_numbers):
    return torch.tensor([dgllife.utils.one_hot_encoding(f, set(allowable_atomic_numbers)) for f in atomic_numbers], dtype=torch.float32)


def form_bond_dict_ordered(offmol):
    """Adds edge types ('atom', 'in[{i}]', 'bond') for i=0,1 and ('bond', 'contains', 'atom')

    to allow bonds to receive indexed messages from neighboring atoms, and atoms to recieve unindexed messages from neighboring bonds
    """
    bonds = [(bond.atom1.molecule_atom_index, bond.atom2.molecule_atom_index) for bond in offmol.bonds]

    bond_dict = dict()
    for atom in range(2):
        bond_dict[('atom', f'in[{atom}]', 'bond')] = []

    reverse_etype = ('bond', 'contains', 'atom')
    bond_dict[reverse_etype] = []

    for atom in range(2):
        forward_etype = ('atom', f'in[{atom}]', 'bond')
        for i in range(len(bonds)):
            bond_dict[forward_etype].append((bonds[i][atom], i))
            bond_dict[reverse_etype].append((i, bonds[i][atom]))

    return bond_dict


def form_angle_dict_ordered(offmol):
    """Adds edge types ('atom', 'in[{i}]', 'angle') for i=0,1,2 and ('angle', 'contains', 'atom')

    to allow angles to receive indexed messages from neighboring atoms, and atoms to recieve unindexed messages from neighboring angles
    """
    angles = [(a.molecule_atom_index, b.molecule_atom_index, c.molecule_atom_index) for (a,b,c) in offmol.angles]

    angle_dict = dict()
    for atom in range(3):
        angle_dict[('atom', f'in[{atom}]', 'angle')] = []

    reverse_etype = ('angle', 'contains', 'atom')
    angle_dict[reverse_etype] = []

    for atom in range(3):
        forward_etype = ('atom', f'in[{atom}]', 'angle')
        for i in range(len(angles)):
            angle_dict[forward_etype].append((angles[i][atom], i))
            angle_dict[reverse_etype].append((i, angles[i][atom]))

    return angle_dict


def form_torsion_dict_ordered(offmol):
    """Adds edge types ('atom', 'in[{i}]', 'torsion') for i=0,1,2,3 and ('torsion', 'contains', 'atom')

    to allow torsions to receive indexed messages from neighboring atoms, and atoms to recieve unindexed messages from neighboring torsions
    """
    torsions = [(a.molecule_atom_index, b.molecule_atom_index, c.molecule_atom_index, d.molecule_atom_index) for (a,b,c,d) in offmol.propers]

    torsion_dict = dict()
    for atom in range(4):
        torsion_dict[('atom', f'in[{atom}]', 'torsion')] = []

    reverse_etype = ('torsion', 'contains', 'atom')
    torsion_dict[reverse_etype] = []

    for atom in range(4):
        forward_etype = ('atom', f'in[{atom}]', 'torsion')
        for i in range(len(torsions)):
            torsion_dict[forward_etype].append((torsions[i][atom], i))
            torsion_dict[reverse_etype].append((i, torsions[i][atom]))

    return torsion_dict


def offmol_to_heterograph(offmol):

    # initialize edges
    data_dict = {}
    data_dict.update(form_bond_dict_ordered(offmol))
    data_dict.update(form_angle_dict_ordered(offmol))
    data_dict.update(form_torsion_dict_ordered(offmol))

    # create factor graph from edge information
    factor_graph = dgl.heterograph(data_dict)

    # initialize atom representation
    atomic_numbers = np.array([atom.atomic_number for atom in offmol.atoms])
    atom_data = one_hot_elements(atomic_numbers)
    factor_graph.nodes['atom'].data['element'] = atom_data

    # initialize factor representation
    # TODO: initialize with other information
    for factor in ['bond', 'angle', 'torsion']:
        N = factor_graph.number_of_nodes(factor)
        factor_graph.nodes[factor].data['initial_repr'] = torch.zeros((N, 1))

    return factor_graph


class MLP(nn.Module):
    """fixed number of hidden units and hidden layers"""
    def __init__(self, in_features, out_features):
        super(MLP, self).__init__()
        self.fc1 = nn.Linear(in_features, 64)
        self.fc2 = nn.Linear(64, 64)
        self.fc3 = nn.Linear(64, out_features)

    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x)


class AtomToFactor(nn.Module):
    def __init__(self, msg_src_name, msg_dest_name, current_repr_name, updated_repr_name, atom_dim,
                 initial_factor_dims=dict(bond=1, angle=1, torsion=1), updated_factor_dims=dict(bond=10, angle=10, torsion=10),
                 symmetrize_torsion=False
                 ):
        """
        Parameters
        ----------
        msg_src_name : string
            name of the message being collected from atoms
        msg_dest_name : string
            name of the message being written on facto
        current_repr_name : string
            name of the current factor representation
        updated_repr_name : string
            name of the factor representation to write into
        atom_dim : int
            dimension of the message being collected from atoms
        initial_factor_dims : dict(string -> int)
            dimensions of the current factor representations
        updated_factor_dims : dict(string -> int)
            dimensions of the updated factor representations
        symmetrize_torsion : bool
            whether to compute torsion representation in way that enforces
            f(a,b,c,d) = f(d,c,b,a)
        """
        super(AtomToFactor, self).__init__()
        self.msg_src_name = msg_src_name
        self.msg_dest_name = msg_dest_name
        self.current_repr_name = current_repr_name
        self.updated_repr_name = updated_repr_name

        self.atom_dim = atom_dim
        self.initial_factor_dims = initial_factor_dims
        self.updated_factor_dims = updated_factor_dims
        self.symmetrize_torsion = symmetrize_torsion


        # bonds
        bond_dim = atom_dim * 2 + initial_factor_dims['bond']
        self.bond_f = MLP(bond_dim, updated_factor_dims['bond'])

        # angles
        angle_dim = atom_dim * 3 + initial_factor_dims['angle']
        self.angle_f = MLP(angle_dim, updated_factor_dims['angle'])

        # torsions
        torsion_dim = atom_dim * 4 + initial_factor_dims['torsion']
        self.torsion_f = MLP(torsion_dim, updated_factor_dims['torsion'])


    # compute updated factor representations based on current incoming messages
    # TODO: reduce code duplication here...

    def _pass_labeled_messages_from_atom_to_bond(self, g):
        """bond nodes will have attributes "{dest}[0]" and "{dest}[1]"
        containing whatever was on atom "src" attribute for
        (atom, in[0], bond) and (atom, in[1], bond) edges, respectively
        """
        N = g.number_of_nodes('bond')
        v = np.arange(N)

        for i in range(2):
            edge_type = ('atom', f'in[{i}]', 'bond')
            destination = f'{self.msg_dest_name}[{i}]'
            g[edge_type].pull(v, fn.copy_u(self.msg_src_name, destination), fn.sum(destination, destination))

    def _pass_labeled_messages_from_atom_to_angle(self, g):
        """angle nodes will have attributes "{dest}[0]", "{dest}[1]", and "{dest}[2]"
        containing whatever was on atom "src" attribute for
        (atom, in[0], angle), (atom, in[1], angle), (atom, in[2], angle) edges, respectively
        """
        N = g.number_of_nodes('angle')
        v = np.arange(N)

        for i in range(3):
            edge_type = ('atom', f'in[{i}]', 'angle')
            destination = f'{self.msg_dest_name}[{i}]'
            g[edge_type].pull(v, fn.copy_u(self.msg_src_name, destination), fn.sum(destination, destination))

    def _pass_labeled_messages_from_atom_to_torsion(self, g):
        """torsion nodes will have attributes "{dest}[0]", "{dest}[1]", "{dest}[2]", "{dest}[3]"
        containing whatever was on atom "src" attribute for
        (atom, in[0], torsion), (atom, in[1], torsion), (atom, in[2], torsion), (atom, in[3], torsion) edges, respectively
        """
        N = g.number_of_nodes('torsion')
        v = np.arange(N)

        for i in range(4):
            edge_type = ('atom', f'in[{i}]', 'torsion')
            destination = f'{self.msg_dest_name}[{i}]'
            g[edge_type].pull(v, fn.copy_u(self.msg_src_name, destination), fn.sum(destination, destination))

    def _compute_updated_bond_representation(self, g):
        """if a bond with current representation r is connected to atoms (a,b,),

        update representation to f(a, b; r) + f(b, a; r)
        """

        current_repr = g.nodes['bond'].data[self.current_repr_name]

        incoming_messages = [g.nodes['bond'].data[f'{self.msg_dest_name}[{i}]'] for i in range(2)]

        X_f = torch.cat(incoming_messages + [current_repr], dim=1)
        X_r = torch.cat(incoming_messages[::-1] + [current_repr], dim=1)

        g.nodes['bond'].data[self.updated_repr_name] = self.bond_f(X_f) + self.bond_f(X_r)

    def _compute_updated_angle_representation(self, g):
        """if an angle with current representation r is connected to atoms (a,b,c),

        update representation to f(a, b, c; r) + f(c, b, a; r)
        """

        current_repr = g.nodes['angle'].data[self.current_repr_name]

        incoming_messages = [g.nodes['angle'].data[f'{self.msg_dest_name}[{i}]'] for i in range(3)]

        X_f = torch.cat(incoming_messages + [current_repr], dim=1)
        X_r = torch.cat(incoming_messages[::-1] + [current_repr], dim=1)

        g.nodes['angle'].data[self.updated_repr_name] = self.angle_f(X_f) + self.angle_f(X_r)

    def _compute_updated_torsion_representation(self, g, symmetrize=False):
        """if a torsion with current representation r is connected to atoms (a,b,c,d),

        symmetrize -> update representation to f(a, b, c, d; r) + f(d, c, b, a; r)
        not symmetrize -> update representation to f(a, b, c, d; r)

        """

        current_repr = g.nodes['torsion'].data[self.current_repr_name]

        incoming_messages = [g.nodes['torsion'].data[f'{self.msg_dest_name}[{i}]'] for i in range(4)]
        X_f = torch.cat(incoming_messages + [current_repr], dim=1)

        if symmetrize:
            X_r = torch.cat(incoming_messages[::-1] + [current_repr], dim=1)
            g.nodes['torsion'].data[self.updated_repr_name] = self.torsion_f(X_f) + self.torsion_f(X_r)
        else:
            g.nodes['torsion'].data[self.updated_repr_name] = self.torsion_f(X_f)

    def forward(self, g):
        self._pass_labeled_messages_from_atom_to_bond(g)
        self._compute_updated_bond_representation(g)

        self._pass_labeled_messages_from_atom_to_angle(g)
        self._compute_updated_angle_representation(g)

        self._pass_labeled_messages_from_atom_to_torsion(g)
        self._compute_updated_torsion_representation(g, symmetrize=self.symmetrize_torsion)


# TODO: Also add rings...

"""
something like, updated_atom_repr = g(\sum_{neighboring_bond_factors} f(factor_repr, atom_repr);  \sum_{neighboring_angle_factors} f(factor_repr, atom_repr); \sum_{neighboring_torsion_factors} f(factor_repr, atom_repr) )
"""

class FactorToAtom(nn.Module):
    def __init__(self, msg_src_name, msg_dest_name, current_repr_name, updated_repr_name, atom_dim, message_dim, updated_atom_dim, factor_dims=dict(bond=1, angle=1, torsion=1)):
        """
        for each factor in ['bond', 'angle', 'torsion']
            {msg_dest_name}_{factor} = \sum_factor factor_f(msg_src_name, current_repr_name)

        msg_dest_name = combine_g({msg_dest_name}_bond; {msg_dest_name}_angle; {msg_dest_name}_torsion)

        """
        super(FactorToAtom, self).__init__()
        self.msg_src_name = msg_src_name
        self.msg_dest_name = msg_dest_name
        self.current_repr_name = current_repr_name
        self.updated_repr_name = updated_repr_name

        self.atom_dim = atom_dim
        self.message_dim = message_dim
        self.updated_atom_dim = updated_atom_dim
        self.factor_dims = factor_dims

        # bonds
        bond_dim = atom_dim + factor_dims['bond']
        self.bond_f = MLP(bond_dim, message_dim)

        # angles
        angle_dim = atom_dim + factor_dims['angle']
        self.angle_f = MLP(angle_dim, message_dim)

        # torsions
        torsion_dim = atom_dim + factor_dims['torsion']
        self.torsion_f = MLP(torsion_dim, message_dim)

        self.fs = dict(bond=self.bond_f, angle=self.angle_f, torsion=self.torsion_f)

        self.combine_g = MLP(3 * message_dim, updated_atom_dim)

    def pass_messages_from_factor_to_atom(self, g, f, factor_repr, atom_repr, atom_dest, edge_type):
        """Compute atom_dest = \sum_factors f(factor_repr; atom_repr)

        edge_type is something like (factor, contains, atom)
        """

        N = g.number_of_nodes('atom')
        v = np.arange(N)

        def message_func(edges):
            x = torch.cat((edges.src[factor_repr], edges.dst[atom_repr]), dim=1)
            return {atom_dest: f(x)}

        # TODO: is there an important difference between push and pull for this step?
        g[edge_type].pull(v, message_func, reduce_func=fn.sum(atom_dest, atom_dest))

    def forward(self, g):
        messages = []
        for factor in ['bond', 'angle', 'torsion']:
            msg_dest = f'{self.msg_dest_name}_{factor}'
            edge_type = (factor, 'contains', 'atom')

            self.pass_messages_from_factor_to_atom(g, self.fs[factor], self.msg_src_name, self.current_repr_name, msg_dest, edge_type)
            messages.append(g.nodes['atom'].data[msg_dest])

        x_combined = torch.cat(messages, dim=1)
        g.nodes['atom'].data[self.updated_repr_name] = self.combine_g(x_combined)


class FactorNet(nn.Module):
    def __init__(self, initial_atom_dim, atom_dim=32, initial_factor_dims=dict(bond=1, angle=1, torsion=1), factor_dims=dict(bond=32, angle=32, torsion=32), message_dim=32, n_rounds=5):
        super(FactorNet, self).__init__()

        self.initial_atom_dim = initial_atom_dim
        self.atom_dim = atom_dim
        self.initial_factor_dims = initial_factor_dims
        self.factor_dims = factor_dims
        self.message_dim = message_dim
        self.n_rounds = n_rounds

        # initial round
        atom_to_factor_models = []
        atom_to_factor_models.append(AtomToFactor(msg_src_name='element', msg_dest_name='incoming_element', current_repr_name='initial_repr', updated_repr_name='round1_repr', atom_dim=initial_atom_dim, initial_factor_dims=initial_factor_dims, updated_factor_dims=factor_dims))

        factor_to_atom_models = []
        factor_to_atom_models.append(FactorToAtom(msg_src_name='round1_repr', msg_dest_name='incoming_round1', current_repr_name='element', updated_repr_name='round1_repr', atom_dim=initial_atom_dim, message_dim=message_dim, updated_atom_dim=atom_dim, factor_dims=factor_dims))

        # subsequent rounds
        for r in range(1, n_rounds):
            # factor -> atom
            factor_to_atom_models.append(FactorToAtom(msg_src_name=f'round{r}_repr', msg_dest_name=f'incoming_round{r+1}', current_repr_name=f'round{r}_repr', updated_repr_name=f'round{r+1}_repr', atom_dim=atom_dim, message_dim=message_dim, updated_atom_dim=atom_dim, factor_dims=factor_dims))

            # atom -> factor
            atom_to_factor_models.append(AtomToFactor(msg_src_name=f'round{r}_repr', msg_dest_name=f'incoming_round{r}_repr', current_repr_name=f'round{r}_repr', updated_repr_name=f'round{r+1}_repr', atom_dim=atom_dim,  initial_factor_dims=factor_dims, updated_factor_dims=factor_dims))

        self.atom_to_factor_models = atom_to_factor_models
        self.factor_to_atom_models = factor_to_atom_models

    def forward(self, g):
        for r in range(self.n_rounds):
            self.atom_to_factor_models[r].forward(g)

            self.factor_to_atom_models[r].forward(g)


def print_fields(factor_graph):
    print('atom representations')
    for name in list(factor_graph.nodes['atom'].data):
        print(f'\t{name}')
    print('factor representations')
    for factor in ['bond', 'angle', 'torsion']:
        print(factor)
        for name in list(factor_graph.nodes[factor].data):
            print(f'\t{name}')

if __name__ == '__main__':

    factor_net = FactorNet(ATOM_DIM, n_rounds=2)

    from espaloma.data.alkethoh.pytorch_datasets import AlkEthOHAngleTypesDataset
    angles_dataset = AlkEthOHAngleTypesDataset()


    # one-hot-ify
    all_angle_labels = set()
    for (_, _, labels) in angles_dataset:
        all_angle_labels.update(set(labels.detach().numpy()))
    all_angle_labels = sorted(list(all_angle_labels))
    n_types = len(all_angle_labels)
    print(all_angle_labels)
    print(n_types)
    to_index = dict(zip(all_angle_labels, range(n_types)))

    def labels_to_tensor(labels):
        labels = labels.detach().numpy()
        return torch.tensor([to_index[l] for l in labels], dtype=torch.int64)

    # convert to heterographs
    print('convert to heterographs')
    from tqdm import tqdm

    graphs = []
    for (mol, inds, labels) in angles_dataset:
        g = offmol_to_heterograph(mol)
        n_angles = g.number_of_nodes('angle')
        #g.nodes['angle'].data['label'] = torch.randint(0, n_types, (n_angles,))
        g.nodes['angle'].data['label'] = labels_to_tensor(labels)
        graphs.append(g)

    print('batching')
    g = dgl.batch_hetero(graphs[:10])
    readout = MLP(factor_net.factor_dims['angle'], n_types)

    from torch.nn import CrossEntropyLoss

    def loss(g):
        factor_net.forward(g)
        # TODO: could measure cross entropy loss from multiple rounds
        preds = readout(g.nodes['angle'].data['round2_repr'])
        target = g.nodes['angle'].data['label']
        return CrossEntropyLoss()(preds, target)

    print('computing loss once')
    L = loss(g)
    print(L)

    print('training with Adam')
    from torch.optim.adam import Adam
    optimizer = Adam(list(readout.parameters()) + list(factor_net.parameters()), lr=1e-3)
    loss_traj = []
    for t in range(1000):
        print('iteration: ', t)
        optimizer.zero_grad()
        batch = dgl.batch_hetero([graphs[i] for i in np.random.randint(0, len(graphs), 100)])
        L = loss(batch)
        L.backward()
        optimizer.step()

        loss_traj.append(L)

        preds = readout(batch.nodes['angle'].data['round2_repr']).argmax(1)
        targets = batch.nodes['angle'].data['label']
        accuracy = np.mean((preds == targets).detach().numpy())
        print('\tloss = {:.3f}'.format(L.detach().numpy()))
        print('\taccuracy = {:.3f}'.format(accuracy))
