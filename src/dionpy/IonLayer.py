from __future__ import annotations

import itertools
from datetime import datetime
from multiprocessing import cpu_count, Pool
from typing import Union, Sequence

import healpy as hp
import numpy as np
from tqdm import tqdm

from .modules.helpers import eval_layer, nan2zero, R_EARTH
from .modules.parallel import iri_star, echaim_star


def _estimate_ahd(htop: float, hint: float = 0, r: float = R_EARTH*1e-3):
    """
    Estimates the angular horizontal distance (ahd) between the top point of an atmospheric
    layer and the Earth's surface.

    :param htop: The height of the top point of the atmospheric layer.
    :param hint: The height above the Earth's surface.
    :param r: The radius of the Earth.
    """
    return np.rad2deg(np.arccos(r / (r + hint)) + np.arccos(r / (r + htop)))


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
    :param rdeg_offset: Extend radius of coordinate plane in [deg].
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
            position: Sequence[float, float, float],
            hbot: float,
            htop: float,
            nlayers: int = 100,
            nside: int = 64,
            rdeg_offset: float = 5,
            pbar: bool = True,
            name: str | None = None,
            iriversion: int = 20,
            autocalc: bool = True,
            echaim: bool = False,
            _pool: Union[Pool, None] = None,
    ):
        # TODO: Set offset to 1 for d-layer
        # TODO: Save a value of rdeg
        self.rdeg = _estimate_ahd(htop, position[-1]) + rdeg_offset

        if echaim:
            if position[0] - self.rdeg < 55:
                raise ValueError(
                    "The E-CHAIM model does not cover all coordinates needed for the ionosphere model at the "
                    "specified instrument's position.")

        self.hbot = hbot
        self.htop = htop
        self.nlayers = nlayers
        self.dt = dt
        self.position = position
        self.name = name
        self.echaim = echaim

        self.nside = nside
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
            self._calc(pbar=pbar, _pool=_pool)

    def _batch_split(self, batch):
        nbatches = len(self._obs_pixels) // batch + 1
        nproc = np.min([cpu_count(), nbatches])
        blat = np.array_split(self._obs_lats, nbatches)
        blon = np.array_split(self._obs_lons, nbatches)
        return nbatches, nproc, blat, blon

    def _calc(self, pbar=True, _pool: Union[Pool, None] = None):
        """
        Makes several calls to iricore in parallel requesting electron density and
        electron temperature for future use in attenuation modeling.
        """

        nbatches, nproc, blat, blon = self._batch_split(200)

        heights = (
            self.hbot,
            self.htop,
            (self.htop - self.hbot) / (self.nlayers - 1) - 1e-6,
        )

        pool = Pool(processes=nproc) if _pool is None else _pool
        res = list(
            tqdm(
                pool.imap(
                    iri_star,
                    zip(
                        itertools.repeat(self.dt),
                        itertools.repeat(heights),
                        blat,
                        blon,
                        itertools.repeat(self.iriversion),
                        # itertools.repeat(aap),
                        # itertools.repeat(af107),
                        # itertools.repeat(nlines),
                    ),
                ),
                total=nbatches,
                disable=not pbar,
                desc=self.name,
            )
        )

        if _pool is None:
            pool.close()

        self.edens = nan2zero(np.vstack([r.edens for r in res]))
        self.etemp = nan2zero(np.vstack([r.etemp for r in res]))
        if self.echaim:
            self._calc_echaim(pbar, _pool)
        return

    def _calc_echaim(self, pbar=True, _pool: Union[Pool, None] = None):
        """
        Replace electron density with that calculated with ECHAIM.
        """
        nbatches, nproc, blat, blon = self._batch_split(100)
        heights = np.linspace(self.hbot, self.htop, self.nlayers, endpoint=True)

        pool = Pool(processes=nproc) if _pool is None else _pool

        res = list(
            tqdm(
                pool.imap(
                    echaim_star,
                    zip(
                        blat,
                        blon,
                        itertools.repeat(heights),
                        itertools.repeat(self.dt),
                        itertools.repeat(True),
                        itertools.repeat(True),
                        itertools.repeat(True),
                    ),
                ),
                total=nbatches,
                disable=not pbar,
                desc=self.name,
            )
        )

        if _pool is None:
            pool.close()

        self.edens = np.vstack(res)
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

    def edll(
            self,
            lat: float | np.ndarray,
            lon: float | np.ndarray,
            layer: int | None = None,
    ) -> float | np.ndarray:
        """
        :param lat: Latitude of a point.
        :param lon: Longitude of a point.
        :param layer: Number of sublayer from the precalculated sublayers.
                      If None - an average over all layers is returned.
        :return: Electron density in the layer.
        """
        map_ = np.zeros(hp.nside2npix(self.nside)) + hp.UNSEEN
        map_[self._obs_pixels] = self.edens[:, layer]
        return hp.pixelfunc.get_interp_val(map_, lon, lat, lonlat=True)

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

    def etll(
            self,
            lat: float | np.ndarray,
            lon: float | np.ndarray,
            layer: int | None = None,
    ) -> float | np.ndarray:
        """
        :param lat: Latitude of a point.
        :param lon: Longitude of a point.
        :param layer: Number of sublayer from the precalculated sublayers.
                      If None - an average over all layers is returned.
        :return: Electron density in the layer.
        """
        map_ = np.zeros(hp.nside2npix(self.nside)) + hp.UNSEEN
        map_[self._obs_pixels] = self.etemp[:, layer]
        return hp.pixelfunc.get_interp_val(map_, lon, lat, lonlat=True)

    def get_heights(self):
        return np.linspace(self.hbot, self.htop, self.nlayers)
