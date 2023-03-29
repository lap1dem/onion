from __future__ import annotations

import itertools
from datetime import datetime
from multiprocessing import cpu_count, Pool
from typing import Tuple, List, Union

import healpy as hp
import iricore
import numpy as np
from tqdm import tqdm

from .modules.helpers import eval_layer
from .modules.parallel import iri_star


class IonLayer:
    """
    A model of a layer of specific height range in the ionosphere. Includes electron density and temperature data after
    calculation.

    :param dt: Date/time of the model.
    :param position: Geographical position of an observer. Must be a tuple containing
                     latitude [deg], longitude [deg], and elevation [m].
    :param hbot: Lower limit in [km] of the D layer of the ionosphere.
    :param htop: Upper limit in [km] of the D layer of the ionosphere.
    :param nlayers: Number of sub-layers in the D layer for intermediate calculations.
    :param nside: Resolution of healpix grid.
    :param rdeg: Radius of disc in [deg] queried to healpy (view distance).
    :param pbar: If True - a progress bar will appear.
    :param name: Name of the layer for description use.
    :param iriversion: Version of the IRI model to use. Must be a two digit integer that refers to
                        the last two digits of the IRI version number. For example, version 20 refers
                        to IRI-2020.
    :param autocalc: If True - the model will be calculated immediately after definition.
    """
    def __init__(
        self,
        dt: datetime,
        position: Tuple[float, float, float],
        hbot: float,
        htop: float,
        nlayers: int = 100,
        nside: int = 64,
        rdeg: float = 20,
        pbar: bool = True,
        name: str | None = None,
        iriversion: int = 20,
        autocalc: bool = True,
        _pool: Union[Pool, None] = None,
        _apf107_args: List | None = None,
    ):
        self.hbot = hbot
        self.htop = htop
        self.nlayers = nlayers
        self.dt = dt
        self.position = position
        self.name = name

        self.nside = nside
        self.rdeg = rdeg
        self.iriversion = iriversion
        self._posvec = hp.ang2vec(self.position[1], self.position[0], lonlat=True)
        self._obs_pixels = hp.query_disc(
            self.nside, self._posvec, np.deg2rad(self.rdeg), inclusive=True
        )
        self._obs_lons, self._obs_lats = hp.pix2ang(
            self.nside, self._obs_pixels, lonlat=True
        )
        self.edens = np.zeros((len(self._obs_pixels), nlayers))
        self.etemp = np.zeros((len(self._obs_pixels), nlayers))

        if autocalc:
            self._calc(pbar=pbar, _pool=_pool, _apf107_args=_apf107_args)

    def _calc(self, pbar=True, _pool: Union[Pool, None] = None, _apf107_args: List | None = None):
        """
        Makes several calls to iricore in parallel requesting electron density and
        electron temperature for future use in attenuation modeling.
        """
        batch = 200
        nbatches = len(self._obs_pixels) // batch + 1
        nproc = np.min([cpu_count(), nbatches])
        blat = np.array_split(self._obs_lats, nbatches)
        blon = np.array_split(self._obs_lons, nbatches)
        heights = (
            self.hbot,
            self.htop,
            (self.htop - self.hbot) / (self.nlayers - 1) - 1e-6,
        )

        if _apf107_args is None:
            aap, af107, nlines = iricore.readapf107(self.iriversion)
        else:
            aap, af107, nlines = _apf107_args

        if _pool is None:
            pool = Pool(processes=nproc)
        else:
            pool = _pool

        res = list(
            tqdm(
                pool.imap(
                    iri_star,
                    zip(
                        itertools.repeat(self.dt),
                        itertools.repeat(heights),
                        blat,
                        blon,
                        itertools.repeat(0.0),
                        itertools.repeat(self.iriversion),
                        itertools.repeat(aap),
                        itertools.repeat(af107),
                        itertools.repeat(nlines),
                    ),
                ),
                total=nbatches,
                disable=not pbar,
                desc=self.name,
            )
        )

        # if self.use_echaim:
        #     alts = np.linspace(self.hbot, self.htop, self.nlayers)
        #     res_echaim = list(
        #         tqdm(
        #             pool.imap(
        #                 echaim_star,
        #                 zip(
        #                     blat,
        #                     blon,
        #                     itertools.repeat(alts),
        #                     itertools.repeat(self.dt),
        #                     itertools.repeat(True),
        #                     itertools.repeat(True),
        #                     itertools.repeat(True),
        #                 ),
        #             ),
        #             total=nbatches,
        #             disable=not pbar,
        #             desc=self.name,
        #         )
        #     )
        #     self.edens = np.vstack(res_echaim)
        if _pool is None:
            pool.close()

        self.edens = np.vstack([r["ne"] for r in res])
        self.etemp = np.vstack([r["te"] for r in res])
        return

    def ed(
        self,
        el: float | np.ndarray,
        az: float | np.ndarray,
        layer: int | None = None,
    ) -> float | np.ndarray:
        """
        :param el: Elevation of an observation.
        :param az: Azimuth of an observation.
        :param layer: Number of sublayer from the precalculated sublayers.
                      If None - an average over all layers is returned.
        :return: Electron density in the layer.
        """
        return eval_layer(
            el,
            az,
            self.nside,
            self.position,
            self.hbot,
            self.htop,
            self.nlayers,
            self._obs_pixels,
            self.edens,
            layer=layer,
        )

    def et(
        self,
        el: float | np.ndarray,
        az: float | np.ndarray,
        layer: int | None = None,
    ) -> float | np.ndarray:
        """
        :param el: Elevation of an observation.
        :param az: Azimuth of an observation.
        :param layer: Number of sublayer from the precalculated sublayers.
                      If None - an average over all layers is returned.
        :return: Electron temperature in the layer.
        """
        return eval_layer(
            el,
            az,
            self.nside,
            self.position,
            self.hbot,
            self.htop,
            self.nlayers,
            self._obs_pixels,
            self.etemp,
            layer=layer,
        )
